import "@cloudscape-design/global-styles/index.css";
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import { applyMode, Mode } from "@cloudscape-design/global-styles";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { isMockMode } from "../lib/config";
import { errorMessage } from "../lib/errors";
import { awsBackend, ViewerBackendContext, type ViewerBackend } from "./backend";
import { loadViewerConfig } from "./config";
import { ViewerSession } from "./session";
import { ViewerApp } from "./ViewerApp";

applyMode(window.matchMedia("(prefers-color-scheme: dark)").matches ? Mode.Dark : Mode.Light);

/** Fixtures in mock mode, otherwise config.json and the deployed APIs. */
async function backend(): Promise<ViewerBackend> {
  if (isMockMode()) {
    const { mockViewerBackend } = await import("./mock");
    return mockViewerBackend();
  }
  return awsBackend(await loadViewerConfig());
}

const container = document.getElementById("root");
if (container) {
  const root = createRoot(container);
  backend().then(
    (value) => {
      root.render(
        <StrictMode>
          <ViewerBackendContext value={value}>
            <ViewerSession>
              <ViewerApp />
            </ViewerSession>
          </ViewerBackendContext>
        </StrictMode>,
      );
    },
    (error: unknown) => {
      root.render(
        <Box padding="xxl">
          <Alert type="error" header="The viewer could not start">
            {errorMessage(error)}. Check that config.json was deployed next to index.html, or run with ?mock for
            fixture data.
          </Alert>
        </Box>,
      );
    },
  );
}
