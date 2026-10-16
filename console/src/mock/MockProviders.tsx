import { useMemo, type ReactNode } from "react";
import { BackendContext, type Backend } from "../lib/backend";
import { SessionContext, type Session } from "../lib/session";
import { mockAgent } from "./agent";
import { MOCK_CONFIG, mockEpg } from "./fixtures";
import { mockGateway } from "./gateway";

const SESSION: Session = {
  user: { name: "Demo operator", email: "operator@example.com", groups: ["fast-operators"] },
  accessToken: () => Promise.resolve("mock-access-token"),
  signOut: () => {
    window.location.reload();
  },
};

/** Session and backend for mock mode: no Cognito, no AWS calls, media from the local mock CDN. */
export function MockProviders({ children }: { children: ReactNode }) {
  const backend = useMemo<Backend>(
    () => ({
      config: MOCK_CONFIG,
      gateway: mockGateway(),
      agent: mockAgent(),
      epg: (channel) => Promise.resolve(mockEpg(channel)),
    }),
    [],
  );
  return (
    <SessionContext value={SESSION}>
      <BackendContext value={backend}>{children}</BackendContext>
    </SessionContext>
  );
}
