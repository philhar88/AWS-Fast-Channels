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

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

/**
 * Asks the Pod Decision Service's {@code /select} which bids form the pod (schema version 1). Pod selection
 * (eligibility, separation, durations, value, standalones) exists once, in the service; the module renders exactly
 * the answer.
 * <p>
 * The outcome is a {@link Selection}, or null with the reason recorded by the caller: a non-200 answer, a body that is
 * not schema version 1, a reference to an unknown bid, a timeout or an unreachable service. The caller then renders
 * the house-only fallback.
 */
public class SelectClient {

    static final long MAX_RESPONSE_BYTES = 256 * 1024;

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

    public Future<Outcome> select(ObjectNode request, Map<String, Candidate> byRef, long remainingMs) {
        final long started = System.nanoTime();
        final long timeout = Math.max(1, Math.min(timeoutMs, remainingMs));
        return httpClient.request(HttpMethod.POST, url, HttpUtil.headers(), request.toString(), timeout,
                        MAX_RESPONSE_BYTES)
                .map(response -> read(response, byRef, started))
                .otherwise(error -> new Outcome(null, "SELECT_UNAVAILABLE " + error.getClass().getSimpleName(),
                        PolicyDecision.millisSince(started)));
    }

    /**
     * The {@code /select} request: the break, its slots and every candidate bid.
     */
    public ObjectNode request(Signals signals, PolicyDecision policy, List<Imp> imps, List<Candidate> candidates) {
        final PolicyPatch patch = policy != null ? policy.patch() : null;
        final ObjectNode request = mapper.createObjectNode()
                .put("v", PolicyPatch.VERSION)
                .put("decision_id", patch != null && patch.decisionId() != null
                        ? patch.decisionId() : signals.defaultDecisionId())
                .put("sid", signals.sid())
                .put("pc", signals.pc())
                .put("evid", signals.evid());
        request.put("dur_ms", signals.durMs());
        request.put("mode", patch != null ? patch.mode().name() : null);
        request.put("policy_fallback", policy != null ? policy.fallback() : "NO_POLICY_HOOK");
        final ArrayNode slots = request.putArray("slots");
        for (Imp imp : imps) {
            final Video video = imp.getVideo();
            final ObjectNode slot = slots.addObject().put("impid", imp.getId());
            slot.put("poddur", video != null ? video.getPoddur() : null);
            slot.set("rqddurs", mapper.valueToTree(video != null ? video.getRqddurs() : null));
        }
        final ArrayNode bids = request.putArray("bids");
        for (Candidate candidate : candidates) {
            final Bid bid = candidate.bid();
            final ObjectNode node = bids.addObject()
                    .put("ref", candidate.ref())
                    .put("bidder", bidder(candidate))
                    .put("seat", candidate.seat())
                    .put("impid", bid.getImpid())
                    .put("price", bid.getPrice());
            node.put("dur", bid.getDur());
            node.put("vast_dur_ms", candidate.vastDurMs());
            node.put("crid", bid.getCrid());
            node.set("adomain", mapper.valueToTree(candidate.adomain()));
            node.set("cat", mapper.valueToTree(bid.getCat() != null ? bid.getCat() : List.of()));
            node.put("cattax", bid.getCattax());
            node.put("dealid", bid.getDealid());
            node.put("house", candidate.house())
                    .put("has_impression", candidate.hasImpression())
                    .put("adm_kind", candidate.kind() == Candidate.Kind.INLINE ? "InLine" : "Wrapper");
        }
        return request;
    }

    private static String bidder(Candidate candidate) {
        final JsonNode ext = candidate.bid().getExt();
        final String adapter = ext != null ? ext.path("prebid").path("meta").path("adaptercode").asText("") : "";
        return adapter.isEmpty() ? candidate.seat() : adapter;
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
     * @throws IllegalArgumentException when it is not schema version 1 or names an unknown bid
     */
    static Selection parse(JsonNode answer, Map<String, Candidate> byRef) {
        if (answer == null || !answer.isObject() || answer.path("v").asInt(-1) != PolicyPatch.VERSION) {
            throw new IllegalArgumentException("unknown schema version");
        }
        final Set<String> used = new HashSet<>();
        final Set<Integer> sequences = new HashSet<>();
        final List<Pick> pod = new ArrayList<>();
        for (JsonNode node : array(answer, "pod")) {
            final int sequence = node.path("sequence").asInt(0);
            require(sequence > 0 && sequences.add(sequence), "pod[].sequence");
            pod.add(pick(node, sequence, byRef, used));
        }
        final List<Pick> standalone = new ArrayList<>();
        for (JsonNode node : array(answer, "standalone")) {
            standalone.add(pick(node, null, byRef, used));
        }
        final Map<String, List<String>> trackers = new LinkedHashMap<>();
        final JsonNode trackerNode = answer.path("trackers");
        if (!trackerNode.isMissingNode() && !trackerNode.isNull()) {
            require(trackerNode.isObject(), "trackers");
            trackerNode.properties().forEach(entry -> {
                require(byRef.containsKey(entry.getKey()) && entry.getValue().isArray(), "trackers." + entry.getKey());
                final List<String> urls = new ArrayList<>();
                entry.getValue().forEach(url -> urls.add(Urls.require(url.asText(""), "trackers[]")));
                trackers.put(entry.getKey(), List.copyOf(urls));
            });
        }
        return new Selection(List.copyOf(pod), List.copyOf(standalone), trackers, answer.path("reason").asText(""));
    }

    private static Pick pick(JsonNode node, Integer sequence, Map<String, Candidate> byRef, Set<String> used) {
        final String ref = node.path("ref").asText("");
        final Candidate candidate = byRef.get(ref);
        require(candidate != null && used.add(ref), "ref " + ref);
        final String impid = node.path("impid").asText(candidate.impid());
        return new Pick(impid, ref, sequence);
    }

    private static List<JsonNode> array(JsonNode answer, String field) {
        final JsonNode node = answer.path(field);
        if (node.isMissingNode() || node.isNull()) {
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

    private static void require(boolean condition, String field) {
        if (!condition) {
            throw new IllegalArgumentException("malformed " + field);
        }
    }
}
