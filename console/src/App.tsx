import AppLayout from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup, { type BreadcrumbGroupProps } from "@cloudscape-design/components/breadcrumb-group";
import Flashbar from "@cloudscape-design/components/flashbar";
import SideNavigation, { type SideNavigationProps } from "@cloudscape-design/components/side-navigation";
import Spinner from "@cloudscape-design/components/spinner";
import TopNavigation, { type TopNavigationProps } from "@cloudscape-design/components/top-navigation";
import { lazy, Suspense, useState } from "react";
import { useBackend } from "./lib/backend";
import { isMockMode } from "./lib/config";
import { useNotifications } from "./lib/notifications";
import { href, useRoute, type Route } from "./lib/router";
import { useSession } from "./lib/session";

const AssistantView = lazy(() => import("./views/AssistantView").then((m) => ({ default: m.AssistantView })));
const ChannelsView = lazy(() => import("./views/ChannelsView").then((m) => ({ default: m.ChannelsView })));
const LibraryView = lazy(() => import("./views/LibraryView").then((m) => ({ default: m.LibraryView })));

const DOCS_URL = "https://github.com/philhar88/AWS-Fast-Channels/tree/main/docs";

/** Application shell: top navigation with the user menu, side navigation, breadcrumbs, notifications, views. */
export function App() {
  const route = useRoute();
  const { config } = useBackend();
  const session = useSession();
  const { items } = useNotifications();
  const [navigationOpen, setNavigationOpen] = useState(true);
  const channels = config.channels.map((channel) => channel.name);

  return (
    <>
      <div id="top-navigation">
        <TopNavigation
          identity={{ href: href({ view: "assistant" }), title: "FAST Channels", logo: { src: "/favicon.svg", alt: "" } }}
          utilities={utilities(session, config.region)}
        />
      </div>
      <AppLayout
        headerSelector="#top-navigation"
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => {
          setNavigationOpen(detail.open);
        }}
        navigation={<Navigation route={route} channels={channels} />}
        breadcrumbs={<BreadcrumbGroup items={breadcrumbs(route)} ariaLabel="Breadcrumbs" />}
        notifications={<Flashbar items={items} stackItems />}
        toolsHide
        maxContentWidth={Number.MAX_VALUE}
        content={
          <Suspense fallback={<Spinner size="large" />}>
            <View route={route} />
          </Suspense>
        }
      />
    </>
  );
}

function View({ route }: { route: Route }) {
  switch (route.view) {
    case "assistant":
      return <AssistantView />;
    case "channels":
      return <ChannelsView key={route.channel ?? ""} channel={route.channel} />;
    case "library":
      return <LibraryView tab={route.tab} />;
  }
}

function Navigation({ route, channels }: { route: Route; channels: readonly string[] }) {
  const items: SideNavigationProps.Item[] = [
    { type: "link", text: "Assistant", href: href({ view: "assistant" }) },
    {
      type: "link-group",
      text: "Channels",
      href: href({ view: "channels" }),
      items: channels.map((channel) => ({ type: "link", text: channel, href: href({ view: "channels", channel }) })),
    },
    { type: "link", text: "Library", href: href({ view: "library", tab: "assets" }) },
    { type: "divider" },
    { type: "link", text: "Documentation", href: DOCS_URL, external: true },
  ];
  const active = route.view === "library" ? href({ view: "library", tab: "assets" }) : href(route);
  return <SideNavigation header={{ text: "Operator console", href: href({ view: "assistant" }) }} items={items} activeHref={active} />;
}

function breadcrumbs(route: Route): BreadcrumbGroupProps.Item[] {
  const home = { text: "FAST Channels", href: href({ view: "assistant" }) };
  switch (route.view) {
    case "assistant":
      return [home, { text: "Assistant", href: href(route) }];
    case "channels": {
      const list = { text: "Channels", href: href({ view: "channels" }) };
      return route.channel ? [home, list, { text: route.channel, href: href(route) }] : [home, list];
    }
    case "library":
      return [home, { text: "Library", href: href(route) }];
  }
}

function utilities(session: ReturnType<typeof useSession>, region: string): TopNavigationProps.Utility[] {
  const groups = session.user.groups.length > 0 ? session.user.groups.join(", ") : "no groups";
  const user: TopNavigationProps.Utility = {
    type: "menu-dropdown",
    text: session.user.name,
    description: session.user.email,
    iconName: "user-profile",
    items: [
      { id: "groups", text: `Groups: ${groups}`, disabled: true },
      { id: "region", text: `Region: ${region}`, disabled: true },
      { id: "sign-out", text: "Sign out" },
    ],
    onItemClick: ({ detail }) => {
      if (detail.id === "sign-out") {
        session.signOut();
      }
    },
  };
  return isMockMode() ? [{ type: "button", text: "Mock data", iconName: "status-warning", ariaLabel: "Mock mode: fixture data, no AWS calls" }, user] : [user];
}
