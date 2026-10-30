package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.fasterxml.jackson.databind.node.TextNode;
import com.iab.openrtb.request.BidRequest;
import io.vertx.core.Future;
import org.prebid.server.auction.CpmRange;
import org.prebid.server.auction.PriceGranularity;
import org.prebid.server.cache.CoreCacheService;
import org.prebid.server.cache.proto.request.bid.BidPutObject;
import org.prebid.server.cache.proto.response.bid.CacheObject;
import org.prebid.server.execution.timeout.Timeout;
import org.prebid.server.proto.openrtb.ext.request.ExtPriceGranularity;
import org.prebid.server.proto.openrtb.ext.request.ExtRequest;
import org.prebid.server.proto.openrtb.ext.request.ExtRequestPrebid;
import org.prebid.server.proto.openrtb.ext.request.ExtRequestTargeting;
import org.prebid.server.settings.model.Account;

import java.math.BigDecimal;
import java.util.List;
import java.util.Objects;
import java.util.Set;

/**
 * The ad-server mode (M8-09): the composed pod is stored in Prebid Cache as one VAST document, and the auction answers
 * with the Prebid targeting an ad server's line items match, so the ad server's VAST redirect creative can point
 * MediaTailor at {@code https://<cache host>/cache?uuid=<hb_uuid>}.
 * <p>
 * A stored request asks for it with {@code ext.fast.pod_cache.ttlseconds} (the entry's lifetime) and sets the price
 * granularity in {@code ext.prebid.targeting}. It never sets {@code ext.prebid.cache}: Prebid Server's core would then
 * store every bid's {@code adm}, which carries neither the pod rules nor our trackers. The entry is written through
 * Prebid Server's own cache client (its {@code cache.internal} endpoint when configured, inside the VPC), and the
 * targeting names its public endpoint ({@code cache.host}, {@code cache.path}).
 * <p>
 * The pod's price is the sum of its winners' prices (a CPM per thousand pods); house ads of the own book are fillers
 * and count for nothing. A pod without a paid ad, a failed write or a missing granularity answers with no bid, so the
 * ad server serves its own house ad.
 */
public class PodCache {

    public static final String FIELD = "pod_cache";
    public static final String BIDDER = "fast-ctv";
    /**
     * The own book's seat: only its {@code ext.fast.house} bids are fillers (a third party could set the flag).
     */
    public static final String OWN_BOOK = "ownbook";
    public static final int MAX_TTL_SECONDS = 3600;
    private static final String VAST_TYPE = "xml";
    private static final String INTEGRATION = "fast-ctv";

    private final CoreCacheService cache;
    private final ObjectMapper mapper;

    /**
     * A request's ask for a cached pod.
     *
     * @param ttlSeconds  how long the cached pod lives (1-3600 s, Prebid Cache's own bounds)
     * @param granularity the price buckets of {@code hb_pb}
     */
    public record Ask(int ttlSeconds, PriceGranularity granularity) {
    }

    /**
     * What the cache returned, ready for the answer.
     *
     * @param uuid   the entry's key (Prebid Cache generates it)
     * @param bucket the pod's price bucket ({@code hb_pb})
     * @param price  the pod's price
     */
    public record Cached(String uuid, String bucket, BigDecimal price) {
    }

    public PodCache(CoreCacheService cache, ObjectMapper mapper) {
        this.cache = Objects.requireNonNull(cache);
        this.mapper = Objects.requireNonNull(mapper);
    }

    /**
     * The pod-cache ask of {@code request} ({@code ext.fast.pod_cache.ttlseconds} with a price granularity in
     * {@code ext.prebid.targeting}), or null when it makes none or an unusable one.
     */
    public Ask ask(BidRequest request) {
        final ExtRequest ext = request != null ? request.getExt() : null;
        return ask(request, ext != null ? ext.getProperty("fast") : null);
    }

    /**
     * The pod-cache ask of {@code request} with its {@code ext.fast} given apart: the policy hook removes it from the
     * request ({@link PodShape}) and hands it on.
     */
    public Ask ask(BidRequest request, JsonNode fast) {
        final ExtRequest ext = request != null ? request.getExt() : null;
        final JsonNode ttl = fast != null ? fast.path(FIELD).path("ttlseconds") : null;
        if (ttl == null || !ttl.canConvertToInt() || ttl.asInt() < 1 || ttl.asInt() > MAX_TTL_SECONDS || ext == null) {
            return null;
        }
        final ExtRequestPrebid prebid = ext.getPrebid();
        final ExtRequestTargeting targeting = prebid != null ? prebid.getTargeting() : null;
        final JsonNode granularity = targeting != null ? targeting.getPricegranularity() : null;
        try {
            if (granularity == null || granularity.isNull()) {
                return null;
            }
            final PriceGranularity buckets = granularity.isTextual()
                    ? PriceGranularity.createFromString(granularity.textValue())
                    : PriceGranularity.createFromExtPriceGranularity(
                            mapper.treeToValue(granularity, ExtPriceGranularity.class));
            return new Ask(ttl.asInt(), buckets);
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * The price of a pod: the sum of its paid winners' prices.
     */
    public static BigDecimal price(List<Candidate> winners) {
        return winners.stream()
                .filter(candidate -> !(candidate.house() && OWN_BOOK.equals(candidate.seat())))
                .map(candidate -> Objects.requireNonNullElse(candidate.bid().getPrice(), BigDecimal.ZERO))
                .reduce(BigDecimal.ZERO, BigDecimal::add);
    }

    /**
     * Stores {@code vast} for {@code ask}'s lifetime; completes with the entry, or null when the pod has no price or
     * its bucket is empty.
     */
    public Future<Cached> put(String vast, BigDecimal price, Ask ask, Account account, Timeout timeout) {
        if (price.signum() <= 0) {
            return Future.succeededFuture(null);
        }
        final String bucket = CpmRange.fromCpm(price, ask.granularity(), account);
        if (CpmRange.DEFAULT_CPM.equals(bucket) || new BigDecimal(bucket).signum() <= 0) {
            return Future.succeededFuture(null);
        }
        final BidPutObject put = BidPutObject.builder()
                .type(VAST_TYPE)
                .value(new TextNode(vast))
                .ttlseconds(ask.ttlSeconds())
                .build();
        return cache.cachePutObjects(List.of(put), false, Set.of(), account != null ? account.getId() : null, null,
                        INTEGRATION, timeout)
                .map(response -> {
                    final List<CacheObject> entries = response != null ? response.getResponses() : null;
                    final String uuid = entries != null && entries.size() == 1 ? entries.getFirst().getUuid() : null;
                    if (uuid == null || uuid.isBlank()) {
                        throw new IllegalStateException("Prebid Cache answered without one uuid");
                    }
                    return new Cached(uuid, bucket, price);
                });
    }

    /**
     * The auction's answer for a cached pod: one bid of {@link #BIDDER} carrying the targeting, or no bid when
     * {@code cached} is null.
     *
     * @param requestId  the bid request's id
     * @param decisionId the break's decision id ({@code <sid>.<evid>}), the bid's id
     * @param impid      the pod's first slot
     * @param ads        how many ads the pod holds (winners and standalones), for the logs
     */
    public ObjectNode answer(String requestId, Cached cached, String decisionId, String impid, int ads) {
        final ObjectNode answer = mapper.createObjectNode().put("id", requestId).put("cur", "USD");
        if (cached == null) {
            answer.putArray("seatbid");
            return answer;
        }
        final ObjectNode targeting = mapper.createObjectNode()
                .put("hb_pb", cached.bucket())
                .put("hb_uuid", cached.uuid())
                .put("hb_cache_id", cached.uuid())
                .put("hb_bidder", BIDDER)
                .put("hb_cache_host", cache.getEndpointHost())
                .put("hb_cache_path", cache.getEndpointPath())
                .put("hb_format", "video");
        final ObjectNode bid = mapper.createObjectNode()
                .put("id", decisionId)
                .put("impid", impid)
                .put("price", cached.price())
                .put("crid", "pod");
        bid.putObject("ext").putObject("prebid").put("type", "video").set("targeting", targeting);
        answer.putArray("seatbid").addObject().put("seat", BIDDER).putArray("bid").add(bid);
        answer.putObject("ext").putObject("fast").put("ads", ads);
        return answer;
    }
}
