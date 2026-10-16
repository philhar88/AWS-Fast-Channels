import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Table, { type TableProps } from "@cloudscape-design/components/table";
import type { AgentSnapshot } from "../lib/agui";
import { formatClock, formatDateTime, formatDuration } from "../lib/format";
import type { Plan, PlannedProgram } from "../lib/schedule";
import { ProgramTable } from "./ProgramTable";

const PLAN_COLUMNS: TableProps.ColumnDefinition<PlannedProgram>[] = [
  { id: "start", header: "Start", cell: (p) => formatClock(p.start) },
  {
    id: "title",
    header: "Title",
    cell: (p) => (
      <>
        <Box>{p.title}</Box>
        <Box variant="small" color="text-body-secondary">
          {p.rating ? `${p.rating} · ${p.asset_id}` : p.asset_id}
        </Box>
      </>
    ),
    isRowHeader: true,
  },
  { id: "duration", header: "On air", cell: (p) => formatDuration(p.duration_ms) },
  { id: "breaks", header: "Breaks", cell: (p) => p.breaks.length },
];

/** The plan or schedule from the agent's latest STATE_SNAPSHOT (CONTRACTS §G). */
export function AgentStatePanel({ snapshot }: { snapshot: AgentSnapshot | null }) {
  if (snapshot?.plan) {
    return <PlanView plan={snapshot.plan} />;
  }
  if (snapshot?.schedule) {
    return (
      <ProgramTable
        variant="container"
        programs={snapshot.schedule.programs}
        header={
          <Header variant="h2" counter={`(${snapshot.schedule.programs.length})`} description={`Published schedule of ${snapshot.channel}`}>
            Schedule
          </Header>
        }
      />
    );
  }
  return (
    <Container header={<Header variant="h2">Plan</Header>}>
      <Box textAlign="center" color="text-body-secondary" padding="l">
        Plans and schedules the agent works on appear here.
      </Box>
    </Container>
  );
}

function PlanView({ plan }: { plan: Plan }) {
  const errors = plan.violations.filter((v) => v.severity === "error").length;
  return (
    <Container
      header={
        <Header
          variant="h2"
          counter={`(${plan.programs.length})`}
          description={`${plan.channel}, ${formatDateTime(plan.start)} to ${formatClock(plan.end)}, ladder ${plan.ladder_version}`}
          info={
            plan.publishable ? (
              <StatusIndicator type="success">Publishable</StatusIndicator>
            ) : (
              <StatusIndicator type="error">{errors} blocking error(s)</StatusIndicator>
            )
          }
        >
          Proposed plan
        </Header>
      }
    >
      <SpaceBetween size="m">
        {plan.violations.map((v) => (
          <Alert key={`${v.code}-${v.program ?? ""}`} type={v.severity === "error" ? "error" : "warning"} header={v.code}>
            {v.message}
          </Alert>
        ))}
        <Table
          variant="embedded"
          items={[...plan.programs]}
          columnDefinitions={PLAN_COLUMNS}
          trackBy="name"
          stripedRows
          ariaLabels={{ tableLabel: "Planned programs" }}
        />
      </SpaceBetween>
    </Container>
  );
}
