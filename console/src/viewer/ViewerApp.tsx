import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import ContentLayout from "@cloudscape-design/components/content-layout";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import TopNavigation from "@cloudscape-design/components/top-navigation";
import { useEffect } from "react";
import { PersonaPicker } from "./components/PersonaPicker";
import { Rails } from "./components/Rails";
import { WatchView } from "./components/WatchView";
import { useViewerRoute, viewerHref } from "./router";
import { useViewer } from "./viewer";

/** The reference viewer: who's watching, their rails, and a channel's player. */
export function ViewerApp() {
  const route = useViewerRoute();
  const viewer = useViewer();
  const persona = viewer.persona;
  const picking = route.view === "personas" || persona === null;

  useEffect(() => {
    document.title = persona ? `${persona.name} · FAST Channels viewer` : "FAST Channels viewer";
  }, [persona]);

  return (
    <>
      <TopNavigation
        identity={{ href: viewerHref({ view: "home" }), title: "FAST Channels · Reference viewer (demo personas)" }}
        utilities={[
          ...(persona
            ? [
                {
                  type: "menu-dropdown" as const,
                  text: persona.name,
                  description: viewer.accessToken ? persona.email ?? undefined : "Guest",
                  iconName: "user-profile" as const,
                  items: [
                    { id: "switch", text: "Switch persona", href: viewerHref({ view: "personas" }) },
                    { id: "signout", text: viewer.accessToken ? "Sign out" : "Leave guest mode" },
                  ],
                  onItemClick: ({ detail }: { detail: { id: string } }) => {
                    if (detail.id === "signout") {
                      viewer.signOut();
                    }
                  },
                },
              ]
            : []),
        ]}
      />
      <Box padding={{ horizontal: "xl", vertical: "l" }}>
        {picking ? (
          <PersonaPicker />
        ) : route.view === "watch" ? (
          <WatchView channel={route.channel} persona={persona} />
        ) : (
          <ContentLayout
            header={
              <Header
                variant="h1"
                info={<Badge color="blue">{viewer.accessToken ? "Signed in" : "Guest"}</Badge>}
                description={persona.bio}
              >
                {persona.name}
              </Header>
            }
          >
            <SpaceBetween size="l">
              <Rails persona={persona} />
            </SpaceBetween>
          </ContentLayout>
        )}
      </Box>
    </>
  );
}
