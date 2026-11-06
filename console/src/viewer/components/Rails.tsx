import Alert from "@cloudscape-design/components/alert";
import Badge from "@cloudscape-design/components/badge";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Spinner from "@cloudscape-design/components/spinner";
import Icon from "@cloudscape-design/components/icon";
import { errorMessage } from "../../lib/errors";
import { formatClock } from "../../lib/format";
import type { Persona } from "../config";
import { channelRail, programRail, type ChannelTile, type ProgramTile } from "../rail";
import { viewerHref } from "../router";
import { useGuides } from "../useGuides";
import styles from "./Rails.module.css";

const watch = (channel: string) => viewerHref({ view: "watch", channel });

/** The persona's rails: recommended channels, then programs coming up in their genres. */
export function Rails({ persona }: { persona: Persona }) {
  const { guides, error, now } = useGuides();
  if (error && !guides) {
    return <Alert type="error" header="The program guide could not be loaded">{errorMessage(error)}</Alert>;
  }
  if (!guides) {
    return <Spinner size="large" />;
  }
  const channels = channelRail(persona.genres, guides, now);
  const programs = programRail(persona.genres, guides, now);
  const forYou = persona.genres.length > 0;
  return (
    <SpaceBetween size="xl">
      <section aria-label="Recommended channels">
        <SpaceBetween size="s">
          <Header
            variant="h2"
            description={
              forYou
                ? `Ranked by ${persona.name}'s genres (${persona.genres.join(", ")}) against what each channel airs now and next. A channel in an ad break now is ranked down.`
                : "No genres declared: channels on air now first."
            }
          >
            {forYou ? `Recommended channels for ${persona.name}` : "Channels on now"}
          </Header>
          <ul className={styles.rail}>
            {channels.map((tile, rank) => (
              <li key={tile.channel.name} className={styles.tile}>
                <ChannelCard tile={tile} rank={rank + 1} now={now} />
              </li>
            ))}
          </ul>
        </SpaceBetween>
      </section>
      <section aria-label="Up next for you">
        <SpaceBetween size="s">
          <Header variant="h2" description="Programs on now or in the next three hours that match the declared genres.">
            {forYou ? "Up next for you" : "Up next"}
          </Header>
          {programs.length === 0 ? (
            <Box color="text-status-inactive">Nothing in these genres in the next three hours.</Box>
          ) : (
            <ul className={styles.rail}>
              {programs.map((tile) => (
                <li key={`${tile.channel.name}/${tile.program.name}`} className={styles.tile}>
                  <ProgramCard tile={tile} />
                </li>
              ))}
            </ul>
          )}
        </SpaceBetween>
      </section>
    </SpaceBetween>
  );
}

function ChannelCard({ tile, rank, now }: { tile: ChannelTile; rank: number; now: number }) {
  return (
    <Container
      header={
        <Header
          variant="h3"
          description={`#${rank}${tile.channel.monetized ? " · monetized" : ""}`}
          actions={
            <Button variant="primary" href={watch(tile.channel.name)} ariaLabel={`Watch ${tile.channel.name}`}>
              Watch
            </Button>
          }
        >
          {tile.channel.name}
        </Header>
      }
    >
      <SpaceBetween size="xs">
        {tile.now ? (
          <Box>
            <Box variant="awsui-key-label">Now</Box>
            {tile.now.title} <Box variant="small">until {formatClock(tile.now.end)}</Box>
          </Box>
        ) : (
          <Box color="text-status-inactive">Nothing on now</Box>
        )}
        {tile.next && (
          <Box>
            <Box variant="awsui-key-label">Next at {formatClock(tile.next.start)}</Box>
            {tile.next.title}
          </Box>
        )}
        {tile.breakEndsAt !== undefined && tile.breakEndsAt > now && <Badge color="red">In a break</Badge>}
        <SpaceBetween size="xxs">
          {tile.reasons.map((reason) => (
            <Box key={reason} color="text-status-info" fontSize="body-s">
              <Icon name="status-info" size="small" /> {reason}
            </Box>
          ))}
        </SpaceBetween>
      </SpaceBetween>
    </Container>
  );
}

function ProgramCard({ tile }: { tile: ProgramTile }) {
  return (
    <Container
      header={
        <Header variant="h3" description={`${tile.channel.name} · ${tile.onNow ? "on now" : formatClock(tile.program.start)}`}>
          {tile.program.title}
        </Header>
      }
      footer={
        <Button href={watch(tile.channel.name)} ariaLabel={`Watch ${tile.program.title} on ${tile.channel.name}`}>
          {tile.onNow ? "Watch now" : "Go to channel"}
        </Button>
      }
    >
      <SpaceBetween direction="horizontal" size="xxs">
        {tile.program.genres.map((genre) => (
          <Badge key={genre} color={tile.matched.includes(genre.toLowerCase()) ? "blue" : "grey"}>
            {genre}
          </Badge>
        ))}
      </SpaceBetween>
    </Container>
  );
}
