package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Video;
import com.iab.openrtb.response.Bid;
import io.vertx.core.Future;
import io.vertx.core.http.HttpMethod;
import org.prebid.server.hooks.modules.fast.ctv.core.Selection.Pick;
import org.prebid.server.util.HttpUtil;
import org.prebid.server.vertx.httpclient.HttpClient;
import org.prebid.server.vertx.httpclient.model.HttpClientResponse;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * Asks the Pod Decision Service's {@code /select} which bids form the pod ({@code contracts/pds-select.schema.json},
 * version 1). Pod selection (eligibility, separation, durations, value, standalones) exists once, in the service; the
 * module renders exactly the answer.
 * <p>
 * The outcome is a {@link Selection}, or null with the reason: a break the request cannot identify, or slots the
 * schema refuses (no call is made, {@code SELECT_SKIPPED <field>}), a non-200 answer, a body that is not the schema
 * (strict: unknown fields refused), a reference to an unknown or unusable bid, a timeout or an unreachable service.
 * The caller then renders the house-only fallback. A bid the schema cannot carry (an invalid bidder or seat name, no
 * price) is left out of the request, so it cannot be placed.
 */
public class SelectClient {

    static final long MAX_RESPONSE_BYTES = 256 * 1024;
    static final int MAX_SLOTS = 16;
    static final int MAX_BIDS = 512;

    private static final Pattern NAME = Pattern.compile("[A-Za-z0-9_.-]{1,64}");
    private static final int MAX_SEAT = 64;
    private static final int MAX_CRID = 256;
    private static final int MAX_DEALID = 128;
    private static final Set<String> FIELDS = Set.of("v", "pod", "standalone", "trackers", "reason");
    private static final Set<String> POD_FIELDS = Set.of("impid", "ref", "sequence");
    private static final Set<String> STANDALONE_FIELDS = Set.of("impid", "ref");
    private static final Set<String> REASONS = Set.of("filled", "partial", "empty", "short_avail");

    private final HttpClient httpClient;
    private final ObjectMapper mapper;
    private final String url;
    private final long timeoutMs;

    public SelectClient(HttpClient httpClient, ObjectMapper mapper, String url, long timeoutMs) {
        this.httpClient = Objects.requireNonNull(httpClient);
        this.mapper = Objects.requireNonNull(mapper);
        this.url = Urls.require(url, "hooks.fast-ctv.select-url");
        this.timeoutMs = timeoutMs;
    }

    /**
     * The outcome of one {@code /select} call: a selection, or the reason there is none.
     */
    public record Outcome(Selection selection, String failure, long latencyMs) {
    }

    /**
     * Asks for the pod of {@code request} ({@link #request}); a null request is not sent ({@code skipped} says
     * why).
     */
    public Future<Outcome> select(ObjectNode request, String skipped, Map<String, Candidate> byRef,
                                  long remainingMs) {
        final long started = System.nanoTime();
        if (request == null) {
            return Future.succeededFuture(new Outcome(null, "SELECT_SKIPPED " + skipped, 0));
        }
        final long timeout = Math.max(1, Math.min(timeoutMs, remainingMs));
        return httpClient.request(HttpMethod.POST, url, HttpUtil.headers(), request.toString(), timeout,
                        MAX_RESPONSE_BYTES)
                .map(response -> read(response, byRef, started))
                .otherwise(error -> new Outcome(null, "SELECT_UNAVAILABLE " + error.getClass().getSimpleName(),
                        PolicyDecision.millisSince(started)));
    }

    /**
     * Why no {@code /select} request can be made for this break, or null: the break is not identified
     * ({@link Signals#problem()}), or a slot has no {@code poddur}, or there are more slots than the schema takes.
     */
    public static String problem(Signals signals, List<Imp> imps) {
        final String signal = signals.problem();
        if (signal != null) {
            return signal;
        }
        if (imps.isEmpty() || imps.size() > MAX_SLOTS) {
            return "slots";
        }
        for (Imp imp : imps) {
            final Video video = imp.getVideo();
            if (imp.getId() == null || !NAME.matcher(imp.getId()).matches()) {
                return "slots[].impid";
            }
            if (video == null || video.getPoddur() == null || video.getPoddur() <= 0) {
                return "slots[].poddur";
            }
        }
        return null;
    }

    /**
     * The {@code /select} request: the break, its slots and every bid the schema can carry (at most
     * {@value #MAX_BIDS}), with what the module read from its {@code adm}. Call only when {@link #problem} is null.
     */
    public ObjectNode request(Signals signals, PolicyDecision policy, List<Imp> imps, List<Candidate> candidates) {
        final PolicyPatch patch = policy != null ? policy.patch() : null;
        final ObjectNode request = mapper.createObjectNode()
                .put("v", PolicyPatch.VERSION)
                .put("decision_id", signals.decisionId())
                .put("sid", signals.sid())
                .put("pc", signals.pc())
                .put("evid", signals.evid());
        request.put("dur_ms", signals.durMs());
        request.put("mode", patch != null ? patch.mode().name() : null);
        request.put("probe", signals.probe());
        final ArrayNode slots = request.putArray("slots");
        for (Imp imp : imps) {
            final Video video = imp.getVideo();
            final ObjectNode slot = slots.addObject().put("impid", imp.getId());
            slot.put("poddur", video.getPoddur());
            final ArrayNode rqddurs = slot.putArray("rqddurs");
            if (video.getRqddurs() != null) {
                video.getRqddurs().stream().filter(dur -> dur != null && dur > 0).forEach(rqddurs::add);
            }
        }
        final ArrayNode bids = request.putArray("bids");
        for (Candidate candidate : candidates) {
            if (bids.size() < MAX_BIDS && carried(candidate)) {
                bids.add(bid(candidate));
            }
        }
        return request;
    }

    /**
     * Whether the schema can carry {@code candidate}: valid bidder, seat and imp names and a price.
     */
    static boolean carried(Candidate candidate) {
        final Bid bid = candidate.bid();
        final String seat = Objects.requireNonNullElse(candidate.seat(), "");
        return NAME.matcher(bidder(candidate)).matches() && seat.length() <= MAX_SEAT
                && bid.getImpid() != null && NAME.matcher(bid.getImpid()).matches()
                && bid.getPrice() != null && bid.getPrice().compareTo(BigDecimal.ZERO) >= 0;
    }

    private ObjectNode bid(Candidate candidate) {
        final Bid bid = candidate.bid();
        final ObjectNode node = mapper.createObjectNode()
                .put("ref", candidate.ref())
                .put("bidder", bidder(candidate))
                .put("seat", Objects.requireNonNullElse(candidate.seat(), ""))
                .put("impid", bid.getImpid())
                .put("price", bid.getPrice());
        node.put("dur", bid.getDur() != null && bid.getDur() >= 0 ? bid.getDur() : null);
        node.put("vast_dur_ms", candidate.vastDurMs());
        node.put("crid", bid.getCrid() != null && bid.getCrid().length() <= MAX_CRID ? bid.getCrid() : null);
        node.set("adomain", strings(bid.getAdomain()));
        node.set("cat", strings(bid.getCat()));
        node.put("cattax", bid.getCattax() != null && bid.getCattax() >= 1 ? bid.getCattax() : null);
        node.put("dealid", bid.getDealid() != null && bid.getDealid().length() <= MAX_DEALID ? bid.getDealid() : null);
        node.put("house", candidate.house())
                .put("has_impression", candidate.hasImpression());
        node.put("adm_kind", candidate.kind() == null ? null
                : candidate.kind() == Candidate.Kind.INLINE ? "InLine" : "Wrapper");
        return node;
    }

    private ArrayNode strings(List<String> values) {
        final ArrayNode array = mapper.createArrayNode();
        if (values != null) {
            values.stream().filter(Objects::nonNull).forEach(array::add);
        }
        return array;
    }

    /**
     * The bidder code of the request: Prebid Server's seat defaults to it ({@code ownbook} for the own book), while
     * {@code ext.prebid.meta.adaptercode} names the adapter behind an alias ({@code generic}), so it is only the
     * fallback.
     */
    private static String bidder(Candidate candidate) {
        if (candidate.seat() != null && !candidate.seat().isEmpty()) {
            return candidate.seat();
        }
        final JsonNode ext = candidate.bid().getExt();
        return ext != null ? ext.path("prebid").path("meta").path("adaptercode").asText("") : "";
    }

    private Outcome read(HttpClientResponse response, Map<String, Candidate> byRef, long started) {
        final long latency = PolicyDecision.millisSince(started);
        if (response.getStatusCode() != 200) {
            return new Outcome(null, "SELECT_HTTP_" + response.getStatusCode(), latency);
        }
        try {
            return new Outcome(parse(mapper.readTree(response.getBody()), byRef), null, latency);
        } catch (Exception e) {
            return new Outcome(null, "SELECT_MALFORMED " + e.getMessage(), latency);
        }
    }

    /**
     * Reads a {@code /select} response.
     *
     * @throws IllegalArgumentException when it is not schema version 1, has a field the schema does not, or names
     *                                  a bid that is unknown, unusable, used twice or placed in another slot
     */
    static Selection parse(JsonNode answer, Map<String, Candidate> byRef) {
        if (answer == null || !answer.isObject() || !answer.path("v").isInt()
                || answer.path("v").asInt() != PolicyPatch.VERSION) {
            throw new IllegalArgumentException("unknown schema version");
        }
        only(answer, FIELDS, "");
        require(answer.path("reason").isTextual() && REASONS.contains(answer.path("reason").asText()), "reason");
        final Set<String> used = new HashSet<>();
        final Set<Integer> sequences = new HashSet<>();
        final List<Pick> pod = new ArrayList<>();
        for (JsonNode node : array(answer, "pod")) {
            only(node, POD_FIELDS, "pod[].");
            final JsonNode sequence = node.path("sequence");
            require(sequence.isInt() && sequence.asInt() > 0 && sequences.add(sequence.asInt()), "pod[].sequence");
            pod.add(pick(node, sequence.asInt(), byRef, used));
        }
        final List<Pick> standalone = new ArrayList<>();
        for (JsonNode node : array(answer, "standalone")) {
            only(node, STANDALONE_FIELDS, "standalone[].");
            standalone.add(pick(node, null, byRef, used));
        }
        final Map<String, List<String>> trackers = new LinkedHashMap<>();
        final JsonNode trackerNode = answer.path("trackers");
        if (!trackerNode.isMissingNode()) {
            require(trackerNode.isObject(), "trackers");
            trackerNode.properties().forEach(entry -> {
                require(used.contains(entry.getKey()) && entry.getValue().isArray(), "trackers." + entry.getKey());
                final List<String> urls = new ArrayList<>();
                entry.getValue().forEach(url -> {
                    require(url.isTextual(), "trackers." + entry.getKey() + "[]");
                    urls.add(Urls.require(url.asText(), "trackers[]"));
                });
                trackers.put(entry.getKey(), List.copyOf(urls));
            });
        }
        return new Selection(List.copyOf(pod), List.copyOf(standalone), trackers, answer.path("reason").asText());
    }

    private static Pick pick(JsonNode node, Integer sequence, Map<String, Candidate> byRef, Set<String> used) {
        final String ref = node.path("ref").asText("");
        final Candidate candidate = byRef.get(ref);
        require(node.path("ref").isTextual() && candidate != null && candidate.usable() && used.add(ref),
                "ref " + ref);
        require(node.path("impid").isTextual() && node.path("impid").asText().equals(candidate.impid()),
                "impid of " + ref);
        return new Pick(candidate.impid(), ref, sequence);
    }

    private static List<JsonNode> array(JsonNode answer, String field) {
        final JsonNode node = answer.path(field);
        if (node.isMissingNode()) {
            return List.of();
        }
        require(node.isArray(), field);
        final List<JsonNode> items = new ArrayList<>();
        node.forEach(item -> {
            require(item.isObject(), field + "[]");
            items.add(item);
        });
        return items;
    }

    private static void only(JsonNode node, Set<String> fields, String prefix) {
        node.fieldNames().forEachRemaining(name -> require(fields.contains(name), prefix + name + " (unknown field)"));
    }

    private static void require(boolean condition, String field) {
        if (!condition) {
            throw new IllegalArgumentException("malformed " + field);
        }
    }
}
