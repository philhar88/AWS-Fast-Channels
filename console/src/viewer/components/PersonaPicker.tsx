import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Cards from "@cloudscape-design/components/cards";
import Form from "@cloudscape-design/components/form";
import FormField from "@cloudscape-design/components/form-field";
import Header from "@cloudscape-design/components/header";
import Input from "@cloudscape-design/components/input";
import Modal from "@cloudscape-design/components/modal";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { useState } from "react";
import { errorMessage } from "../../lib/errors";
import { useViewerBackend } from "../backend";
import type { Persona } from "../config";
import { viewerHref } from "../router";
import { useViewer } from "../viewer";

const KIND: Record<Persona["signIn"], string> = {
  user: "Viewer account",
  kids_profile: "Kids profile",
  guest: "Guest, no sign-in",
};

/** Who's watching: each persona signs in as its own viewer-pool user, or watches as a guest. */
export function PersonaPicker() {
  const { config } = useViewerBackend();
  const viewer = useViewer();
  const [signingIn, setSigningIn] = useState<Persona | null>(null);

  const choose = (persona: Persona) => {
    if (persona.signIn === "guest") {
      viewer.watchAsGuest(persona);
      window.location.hash = viewerHref({ view: "home" });
    } else {
      setSigningIn(persona);
    }
  };

  return (
    <SpaceBetween size="l">
      <Cards
        header={
          <Header
            variant="h1"
            description="Demo personas. Each signs in as its own viewer, so the playback API, the ad decisions and the rail see a different person. Open each persona in its own window to compare them side by side."
          >
            Who's watching?
          </Header>
        }
        items={config.personas}
        trackBy="id"
        cardsPerRow={[{ cards: 1 }, { minWidth: 600, cards: 2 }, { minWidth: 1000, cards: 3 }]}
        cardDefinition={{
          header: (persona) => persona.name,
          sections: [
            { id: "bio", content: (persona) => persona.bio },
            {
              id: "genres",
              header: "Likes",
              content: (persona) =>
                persona.genres.length > 0 ? (
                  <SpaceBetween direction="horizontal" size="xxs">
                    {persona.genres.map((genre) => (
                      <Badge key={genre}>{genre}</Badge>
                    ))}
                  </SpaceBetween>
                ) : (
                  <Box color="text-status-inactive">Nothing declared</Box>
                ),
            },
            { id: "kind", header: "Watches as", content: (persona) => KIND[persona.signIn] },
            {
              id: "action",
              content: (persona) =>
                persona.signIn === "kids_profile" ? (
                  <Box color="text-status-inactive">Kids profiles need a child-directed channel, which this demo does not have.</Box>
                ) : (
                  <Button
                    variant={viewer.persona?.id === persona.id ? "normal" : "primary"}
                    ariaLabel={`Watch as ${persona.name}`}
                    onClick={() => {
                      choose(persona);
                    }}
                  >
                    {viewer.persona?.id === persona.id ? "Watching now" : persona.signIn === "guest" ? "Watch as guest" : "Sign in"}
                  </Button>
                ),
            },
          ],
        }}
      />
      {signingIn && (
        <SignInModal
          persona={signingIn}
          onClose={() => {
            setSigningIn(null);
          }}
        />
      )}
    </SpaceBetween>
  );
}

function SignInModal({ persona, onClose }: { persona: Persona; onClose: () => void }) {
  const viewer = useViewer();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = () => {
    setBusy(true);
    setError(null);
    viewer.signIn(persona, password).then(
      () => {
        onClose();
        window.location.hash = viewerHref({ view: "home" });
      },
      (failure: unknown) => {
        setError(errorMessage(failure));
        setBusy(false);
      },
    );
  };

  return (
    <Modal visible header={`Sign in as ${persona.name}`} onDismiss={onClose}>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <Form
          actions={
            <SpaceBetween direction="horizontal" size="xs">
              <Button formAction="none" variant="link" onClick={onClose}>
                Cancel
              </Button>
              <Button variant="primary" loading={busy} disabled={password === ""}>
                Sign in
              </Button>
            </SpaceBetween>
          }
        >
          <SpaceBetween size="m">
            <FormField label="Email">
              <Input value={persona.email ?? ""} readOnly ariaLabel="Email" onChange={() => undefined} />
            </FormField>
            <FormField label="Password" description="From the demo users' secret (docs/viewer.md)">
              <Input
                type="password"
                value={password}
                autoFocus
                ariaLabel="Password"
                onChange={({ detail }) => {
                  setPassword(detail.value);
                }}
              />
            </FormField>
            {error && <Alert type="error">{error}</Alert>}
          </SpaceBetween>
        </Form>
      </form>
    </Modal>
  );
}
