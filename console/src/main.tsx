import "@cloudscape-design/global-styles/index.css";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import { applyMode, Mode } from "@cloudscape-design/global-styles";
import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { AwsBackend } from "./components/AwsBackend";
import { CognitoSession } from "./components/CognitoSession";
import { NotificationsProvider } from "./components/NotificationsProvider";
import { isMockMode, loadConfig } from "./lib/config";
import { errorMessage } from "./lib/errors";

applyMode(window.matchMedia("(prefers-color-scheme: dark)").matches ? Mode.Dark : Mode.Light);

/** The provider stack: fixtures in mock mode, otherwise config.json + Cognito + the deployed backend. */
async function providers(app: ReactNode): Promise<ReactNode> {
  if (isMockMode()) {
    const { MockProviders } = await import("./mock/MockProviders");
    return <MockProviders>{app}</MockProviders>;
  }
  const config = await loadConfig();
  return (
    <CognitoSession config={config}>
      <AwsBackend config={config}>{app}</AwsBackend>
    </CognitoSession>
  );
}

const container = document.getElementById("root");
if (container) {
  const root = createRoot(container);
  providers(<App />).then(
    (tree) => {
      root.render(
        <StrictMode>
          <NotificationsProvider>{tree}</NotificationsProvider>
        </StrictMode>,
      );
    },
    (error: unknown) => {
      root.render(
        <Box padding="xxl">
          <Alert type="error" header="The console could not start">
            {errorMessage(error)}. Check that config.json was deployed next to
            index.html, or run with ?mock for fixture data.
          </Alert>
        </Box>,
      );
    },
  );
}
