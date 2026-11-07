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
import { describeRail, type RailAnswer, type RailTile as RailTileAnswer } from "../railClient";
import { viewerHref } from "../router";
import { useGuides } from "../useGuides";
import { useRail } from "../useRail";
import styles from "./Rails.module.css";

const watch = (channel: string) => viewerHref({ view: "watch", channel });

/** The persona's rails: recommended channels, then the recommender's rail (or programs coming up in their genres
 * when the recs module is not deployed). */
export function Rails({ persona }: { persona: Persona }) {
  const { guides, error, now } = useGuides();
  const rail = useRail(persona.signIn === "kids_profile");
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
      {rail.enabled ? (
        <RecommendedRail answer={rail.answer} error={rail.error} fallback={programs} />
      ) : (
        <ProgramRail title={forYou ? "Up next for you" : "Up next"} programs={programs} />
      )}
    </SpaceBetween>
  );
}

/** The local rule-based rail (rail.ts `programRail`). */
function ProgramRail({ title, programs }: { title: string; programs: readonly ProgramTile[] }) {
  return (
    <section aria-label="Up next for you">
      <SpaceBetween size="s">
        <Header variant="h2" description="Programs on now or in the next three hours that match the declared genres.">
          {title}
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
  );
}

/**
 * The recommender's rail (`GET /rail`): its title, how it was made and why, and each tile's reason. While it loads,
 * and if it fails, the local rule-based rail stands in.
 */
function RecommendedRail({
  answer,
  error,
  fallback,
}: {
  answer: RailAnswer | undefined;
  error: unknown;
  fallback: readonly ProgramTile[];
}) {
  if (!answer) {
    return (
      <SpaceBetween size="s">
        {error !== undefined && (
          <Alert type="info" header="The recommender is unavailable">
            {errorMessage(error)}. Showing the rule-based rail.
          </Alert>
        )}
        <ProgramRail title="Up next for you" programs={fallback} />
      </SpaceBetween>
    );
  }
  return (
    <section aria-label="Recommended for you">
      <SpaceBetween size="s">
        <Header
          variant="h2"
          description={describeRail(answer)}
          info={<Badge color={answer.strategy === "llm" ? "blue" : "grey"}>{answer.strategy}</Badge>}
        >
          {answer.title}
        </Header>
        {answer.tiles.length === 0 ? (
          <Box color="text-status-inactive">Nothing to recommend right now.</Box>
        ) : (
          <ul className={styles.rail}>
            {answer.tiles.map((tile) => (
              <li key={`${tile.channel}/${tile.program}`} className={styles.tile}>
                <RecommendedCard tile={tile} />
              </li>
            ))}
          </ul>
        )}
      </SpaceBetween>
    </section>
  );
}

function RecommendedCard({ tile }: { tile: RailTileAnswer }) {
  return (
    <Container
      header={
        <Header variant="h3" description={`${tile.channel} · ${tile.on_now ? "on now" : formatClock(tile.start)}`}>
          {tile.title}
        </Header>
      }
      footer={
        <Button href={watch(tile.channel)} ariaLabel={`Watch ${tile.title} on ${tile.channel}`}>
          {tile.on_now ? "Watch now" : "Go to channel"}
        </Button>
      }
    >
      <Box color="text-status-info" fontSize="body-s">
        <Icon name="status-info" size="small" /> {tile.reason}
      </Box>
    </Container>
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
