import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Toggle from "@cloudscape-design/components/toggle";
import { useEffect, useState } from "react";
import { errorMessage } from "../../lib/errors";
import { useViewerBackend } from "../backend";
import { modeOf, type ConsentChoice, type ConsentMode, type ConsentRecord } from "../consent";
import { globalPrivacyControl } from "../playback";
import { viewerHref } from "../router";
import { useViewer } from "../viewer";

const MEANING: Record<ConsentMode, string> = {
  FULL: "Ads may use a pseudonymous id per partner and what you watch.",
  PSEUDONYMOUS: "Ads use what the show is about, never an id or the title you watch.",
  CONTEXTUAL: "Ads use only what the show is about: no id or data about you reaches any bidder.",
  SAFE: "Until you choose, you get only this service's own and house ads.",
};

type Loaded = { readonly state: "loading" } | { readonly state: "ready"; readonly record: ConsentRecord | null } | { readonly state: "failed"; readonly error: string };

/** The signed-in viewer's consent, read once per mount. */
function useConsent(): [Loaded, (record: ConsentRecord) => void] {
  const backend = useViewerBackend();
  const { accessToken } = useViewer();
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  useEffect(() => {
    if (!accessToken) {
      return;
    }
    const controller = new AbortController();
    accessToken()
      .then((token) => backend.consent(token, controller.signal))
      .then(
        (record) => {
          setLoaded({ state: "ready", record });
        },
        (error: unknown) => {
          if (!controller.signal.aborted) {
            setLoaded({ state: "failed", error: errorMessage(error) });
          }
        },
      );
    return () => {
      controller.abort();
    };
  }, [accessToken, backend]);
  return [loaded, (record) => {
    setLoaded({ state: "ready", record });
  }];
}

/** On the home page: a signed-in viewer who has not chosen yet is told so, with a link to choose. */
export function ConsentNotice() {
  const { accessToken } = useViewer();
  const [loaded] = useConsent();
  if (!accessToken || loaded.state !== "ready" || loaded.record !== null) {
    return null;
  }
  return (
    <Alert type="info" header="Choose your privacy settings" action={<Button href={viewerHref({ view: "privacy" })}>Privacy choices</Button>}>
      {MEANING.SAFE}
    </Alert>
  );
}

/** `#/privacy`: the signed-in viewer's two choices (identity module, `PUT /consent`); a guest's browser signal. */
export function PrivacyView() {
  const backend = useViewerBackend();
  const { accessToken } = useViewer();
  const [loaded, saved] = useConsent();
  const [choice, setChoice] = useState<ConsentChoice | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!accessToken) {
    return (
      <Container header={<Header variant="h1">Privacy choices</Header>}>
        <SpaceBetween size="s">
          <Box>A guest's ads use only what the show is about: no id or data about you reaches any bidder.</Box>
          <Box>
            Global Privacy Control is {globalPrivacyControl() ? "on" : "off"} in this browser; with it on, your plays are also recorded as opted out.
          </Box>
          <Link href={viewerHref({ view: "home" })}>Back</Link>
        </SpaceBetween>
      </Container>
    );
  }
  if (loaded.state === "loading") {
    return <Box>Loading your privacy choices…</Box>;
  }
  if (loaded.state === "failed") {
    return <Alert type="error">{loaded.error}</Alert>;
  }
  const current = choice ?? { opt_out: loaded.record?.opt_out ?? false, vppa: loaded.record?.vppa ?? false };
  const save = () => {
    setBusy(true);
    setError(null);
    accessToken()
      .then((token) => backend.setConsent(token, current))
      .then(
        (record) => {
          saved(record);
          setChoice(null);
          setBusy(false);
        },
        (failure: unknown) => {
          setError(errorMessage(failure));
          setBusy(false);
        },
      );
  };
  const mode = modeOf(loaded.record);
  return (
    <Container
      header={
        <Header variant="h1" description={`Your breaks now: ${mode}. ${MEANING[mode]}`}>
          Privacy choices
        </Header>
      }
    >
      <SpaceBetween size="m">
        <Toggle
          checked={current.opt_out}
          onChange={({ detail }) => {
            setChoice({ ...current, opt_out: detail.checked });
          }}
        >
          Do not sell or share my personal information
        </Toggle>
        <Toggle
          checked={current.vppa}
          disabled={current.opt_out}
          onChange={({ detail }) => {
            setChoice({ ...current, vppa: detail.checked });
          }}
        >
          Share what I watch with ad partners (video privacy consent)
        </Toggle>
        <Box variant="small">With this choice: {MEANING[modeOf(current)]} A change reaches your next ad break within seconds.</Box>
        {error && <Alert type="error">{error}</Alert>}
        <SpaceBetween direction="horizontal" size="xs">
          <Button variant="primary" loading={busy} onClick={save}>
            Save
          </Button>
          <Link href={viewerHref({ view: "home" })}>Back</Link>
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  );
}
