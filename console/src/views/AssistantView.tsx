import { Avatar, ChatBubble, SupportPromptGroup } from "@cloudscape-design/chat-components";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ContentLayout from "@cloudscape-design/components/content-layout";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Grid from "@cloudscape-design/components/grid";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import LiveRegion from "@cloudscape-design/components/live-region";
import PromptInput from "@cloudscape-design/components/prompt-input";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator, { type StatusIndicatorProps } from "@cloudscape-design/components/status-indicator";
import { useEffect, useRef, useState } from "react";
import { AgentMarkdown } from "../components/AgentMarkdown";
import { AgentStatePanel } from "../components/AgentStatePanel";
import { JsonBlock } from "../components/JsonBlock";
import type { ApprovalDecision } from "../lib/agui";
import { useBackend } from "../lib/backend";
import { formatUsd } from "../lib/format";
import { useSession } from "../lib/session";
import { pendingApproval, type ToolStatus, type TranscriptItem } from "../lib/transcript";
import { useAgentChat } from "../lib/useAgentChat";
import styles from "./AssistantView.module.css";

const SUGGESTIONS = [
  { id: "plan", text: "Fill fast-demo for the next two hours with family titles" },
  { id: "status", text: "Which channels are on air right now?" },
  { id: "ladder", text: "Propose a cheaper ladder without the 1080p rung and estimate its cost" },
];

/** Chat with the scheduling agent (AG-UI over AgentCore Runtime). CONFIRM tools stop at an approval card. */
export function AssistantView() {
  const { agent } = useBackend();
  const chat = useAgentChat(agent);
  const { transcript } = chat;
  const [prompt, setPrompt] = useState("");
  const pending = pendingApproval(transcript);
  const conversation = useRef<HTMLDivElement>(null);

  // Follow the stream inside the pane without moving the page.
  useEffect(() => {
    conversation.current?.scrollTo({ top: conversation.current.scrollHeight });
  }, [transcript.items]);

  const send = (text: string) => {
    if (text.trim()) {
      chat.send(text.trim());
      setPrompt("");
    }
  };

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Plan, publish and inspect channel schedules in natural language. The agent asks for approval before it changes anything on air."
          actions={
            <Button iconName="add-plus" onClick={chat.reset} disabled={transcript.items.length === 0}>
              New conversation
            </Button>
          }
        >
          Scheduling assistant
        </Header>
      }
    >
      <Grid gridDefinition={[{ colspan: { default: 12, s: 7 } }, { colspan: { default: 12, s: 5 } }]}>
        <Container
          header={<Header variant="h2" description={`Session ${chat.sessionId}`}>Conversation</Header>}
          footer={
            <PromptInput
              value={prompt}
              onChange={({ detail }) => {
                setPrompt(detail.value);
              }}
              onAction={() => {
                send(prompt);
              }}
              actionButtonIconName="send"
              i18nStrings={{ actionButtonAriaLabel: "Send" }}
              ariaLabel="Message to the scheduling agent"
              placeholder={pending ? "Approve or reject the pending action first" : "Ask the scheduling agent"}
              disabled={chat.busy || pending !== undefined}
              disableActionButton={!prompt.trim()}
              maxRows={6}
            />
          }
        >
          <div ref={conversation} className={styles.conversation} role="region" aria-label="Conversation">
            {transcript.items.length === 0 ? (
              <SpaceBetween size="m">
                <Box color="text-body-secondary">Start with one of these, or ask your own question.</Box>
                <SupportPromptGroup
                  ariaLabel="Suggested prompts"
                  items={SUGGESTIONS}
                  onItemClick={({ detail }) => {
                    send(SUGGESTIONS.find((s) => s.id === detail.id)?.text ?? "");
                  }}
                />
              </SpaceBetween>
            ) : (
              <SpaceBetween size="m">
                {transcript.items.map((item, index) => (
                  <Message
                    key={item.id}
                    item={item}
                    continued={index > 0 && isAgent(item) && isAgent(transcript.items[index - 1])}
                    busy={chat.busy}
                    onDecide={chat.decide}
                  />
                ))}
                {transcript.running && !transcript.items.some((i) => i.kind === "assistant" && i.streaming) && (
                  <ChatBubble type="incoming" ariaLabel="Agent is working" avatar={<AgentAvatar loading />} showLoadingBar>
                    <Box color="text-status-inactive">Working…</Box>
                  </ChatBubble>
                )}
              </SpaceBetween>
            )}
          </div>
          <LiveRegion hidden>
            {transcript.running ? "The agent is responding" : pending ? "The agent needs your approval" : ""}
          </LiveRegion>
        </Container>
        <AgentStatePanel snapshot={transcript.snapshot} />
      </Grid>
    </ContentLayout>
  );
}

const isAgent = (item: TranscriptItem | undefined): boolean => item !== undefined && item.kind !== "user";

function AgentAvatar({ loading = false }: { loading?: boolean }) {
  return <Avatar color="gen-ai" iconName="gen-ai" ariaLabel="Scheduling agent" tooltipText="Scheduling agent" loading={loading} />;
}

function UserAvatar() {
  const { user } = useSession();
  const initials = user.name.split(/[\s@.]+/).filter(Boolean).slice(0, 2).map((part) => part[0]?.toUpperCase()).join("");
  return <Avatar initials={initials} ariaLabel={user.name} tooltipText={user.name} />;
}

interface MessageProps {
  readonly item: TranscriptItem;
  /** Same author as the previous item: keep the avatar's space but hide it. */
  readonly continued: boolean;
  /** A run's stream is open: decisions wait until it closes (one run at a time per session). */
  readonly busy: boolean;
  readonly onDecide: (decision: ApprovalDecision) => void;
}

function Message({ item, continued, busy, onDecide }: MessageProps) {
  switch (item.kind) {
    case "user":
      return (
        <ChatBubble type="outgoing" ariaLabel="You" avatar={<UserAvatar />}>
          {item.text}
        </ChatBubble>
      );
    case "assistant":
      return (
        <ChatBubble type="incoming" ariaLabel="Scheduling agent" avatar={<AgentAvatar loading={item.streaming} />} hideAvatar={continued}>
          <AgentMarkdown text={item.text} />
        </ChatBubble>
      );
    case "tool":
      return (
        <ChatBubble type="incoming" ariaLabel={`Tool call ${item.name}`} avatar={<AgentAvatar />} hideAvatar={continued}>
          <ExpandableSection
            variant="footer"
            headerText={
              <StatusIndicator type={TOOL_STATUS[item.status].type} iconAriaLabel={TOOL_STATUS[item.status].label}>
                {item.name}
              </StatusIndicator>
            }
          >
            <SpaceBetween size="xs">
              <JsonBlock label="Arguments" text={item.args} />
              {item.result !== undefined && <JsonBlock label="Result" text={item.result} />}
            </SpaceBetween>
          </ExpandableSection>
        </ChatBubble>
      );
    case "approval":
      return (
        <ChatBubble type="incoming" ariaLabel={`Approval required for ${item.request.tool}`} avatar={<AgentAvatar />} hideAvatar={continued}>
          <ApprovalCard item={item} busy={busy} onDecide={onDecide} />
        </ChatBubble>
      );
    case "error":
      return (
        <Alert type="error" header="The agent run failed">
          {item.message}
        </Alert>
      );
  }
}

const TOOL_STATUS: Record<ToolStatus, { readonly type: StatusIndicatorProps.Type; readonly label: string }> = {
  running: { type: "in-progress", label: "Running" },
  succeeded: { type: "success", label: "Succeeded" },
  failed: { type: "error", label: "Failed" },
  declined: { type: "stopped", label: "Declined" },
};

interface ApprovalCardProps {
  readonly item: Extract<TranscriptItem, { kind: "approval" }>;
  readonly busy: boolean;
  readonly onDecide: (decision: ApprovalDecision) => void;
}

function ApprovalCard({ item, busy, onDecide }: ApprovalCardProps) {
  const [note, setNote] = useState("");
  const { request, decision } = item;
  const decide = (approved: boolean) => {
    onDecide({ interrupt_id: request.interrupt_id, approved, note: note.trim() || null });
  };
  return (
    <SpaceBetween size="s">
      <Box variant="h4">Approval required</Box>
      <Box>{request.reason}</Box>
      <KeyValuePairs
        columns={2}
        items={[
          { label: "Tool", value: <Box variant="code">{request.tool}</Box> },
          // The agent narrates the costs the tools return; an estimate in the event itself is optional (CONTRACTS §G).
          ...(request.cost_estimate_usd === undefined ? [] : [{ label: "Estimated cost", value: formatUsd(request.cost_estimate_usd) }]),
        ]}
      />
      <JsonBlock label="Arguments" text={JSON.stringify(request.arguments)} />
      {decision ? (
        <StatusIndicator type={decision.approved ? "success" : "stopped"}>
          {decision.approved ? "Approved" : "Rejected"}
          {decision.note ? `: ${decision.note}` : ""}
        </StatusIndicator>
      ) : (
        <SpaceBetween size="xs">
          <Input
            value={note}
            onChange={({ detail }) => {
              setNote(detail.value);
            }}
            placeholder="Note for the agent (optional)"
            ariaLabel="Note for the agent"
          />
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              disabled={busy}
              onClick={() => {
                decide(false);
              }}
            >
              Reject
            </Button>
            <Button
              variant="primary"
              disabled={busy}
              onClick={() => {
                decide(true);
              }}
            >
              Approve
            </Button>
          </SpaceBetween>
        </SpaceBetween>
      )}
    </SpaceBetween>
  );
}
