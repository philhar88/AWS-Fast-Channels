import { useMemo, type ReactNode } from "react";
import { awsBackend, BackendContext } from "../lib/backend";
import type { AppConfig } from "../lib/config";
import { useSession } from "../lib/session";

/** Provides the deployed backend (Runtime, Gateway, EPG feed), authorised by the signed-in user's token. */
export function AwsBackend({ config, children }: { config: AppConfig; children: ReactNode }) {
  const { accessToken } = useSession();
  const backend = useMemo(() => awsBackend(config, accessToken), [config, accessToken]);
  return <BackendContext value={backend}>{children}</BackendContext>;
}
