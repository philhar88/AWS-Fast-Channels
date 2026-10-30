package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.fasterxml.jackson.databind.node.TextNode;
import com.iab.openrtb.request.BidRequest;
import io.vertx.core.Future;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.prebid.server.cache.CoreCacheService;
import org.prebid.server.cache.proto.request.bid.BidPutObject;
import org.prebid.server.cache.proto.response.bid.BidCacheResponse;
import org.prebid.server.cache.proto.response.bid.CacheObject;
import org.prebid.server.execution.timeout.Timeout;
import org.prebid.server.execution.timeout.TimeoutFactory;
import org.prebid.server.proto.openrtb.ext.request.ExtRequest;
import org.prebid.server.proto.openrtb.ext.request.ExtRequestPrebid;
import org.prebid.server.proto.openrtb.ext.request.ExtRequestTargeting;
import org.prebid.server.settings.model.Account;

import java.math.BigDecimal;
import java.time.Clock;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anySet;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.bid;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.house;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;

public class PodCacheTest {

    /**
     * The ad-server stored requests' price granularity: 0.10 to 20, 0.50 to 50, 1.00 to 100 (CTV CPMs).
     */
    public static final JsonNode CTV_GRANULARITY = MAPPER.createObjectNode().put("precision", 2).set("ranges",
            MAPPER.createArrayNode()
                    .add(MAPPER.createObjectNode().put("max", 20).put("increment", 0.10))
                    .add(MAPPER.createObjectNode().put("max", 50).put("increment", 0.50))
                    .add(MAPPER.createObjectNode().put("max", 100).put("increment", 1.00)));

    private final CoreCacheService cacheService = mock(CoreCacheService.class);
    private final PodCache target = new PodCache(cacheService, MAPPER);
    private final Timeout timeout = new TimeoutFactory(Clock.systemUTC()).create(150);
    private final Account account = Account.empty("fastm");

    /**
     * {@code pod(slots)} as an ad-server stored request holds it: a pod-cache TTL and a price granularity.
     */
    public static BidRequest adServerPod(int slots, JsonNode ttl, JsonNode granularity) {
        final ExtRequest ext = ExtRequest.of(ExtRequestPrebid.builder()
                .targeting(ExtRequestTargeting.builder().pricegranularity(granularity).build()).build());
        if (ttl != null) {
            ext.addProperty("fast", MAPPER.createObjectNode().set(PodCache.FIELD,
                    MAPPER.createObjectNode().set("ttlseconds", ttl)));
        }
        return pod(slots).toBuilder().ext(ext).build();
    }

    @Test
    public void aStoredRequestAsksWithATtlAndAGranularity() {
        final PodCache.Ask ask = target.ask(adServerPod(2, MAPPER.getNodeFactory().numberNode(300), CTV_GRANULARITY));

        assertThat(ask).isNotNull();
        assertThat(ask.ttlSeconds()).isEqualTo(300);
        assertThat(ask.granularity().getPrecision()).isEqualTo(2);
        assertThat(target.ask(adServerPod(1, MAPPER.getNodeFactory().numberNode(60), new TextNode("med"))))
                .isNotNull();
    }

    @Test
    public void anythingElseIsNoAsk() {
        final JsonNode ttl = MAPPER.getNodeFactory().numberNode(300);
        assertThat(target.ask(pod(2))).isNull();
        assertThat(target.ask(null)).isNull();
        assertThat(target.ask(adServerPod(2, null, CTV_GRANULARITY))).isNull();
        assertThat(target.ask(adServerPod(2, MAPPER.getNodeFactory().numberNode(0), CTV_GRANULARITY))).isNull();
        assertThat(target.ask(adServerPod(2, MAPPER.getNodeFactory().numberNode(3601), CTV_GRANULARITY))).isNull();
        assertThat(target.ask(adServerPod(2, new TextNode("300"), CTV_GRANULARITY))).isNull();
        assertThat(target.ask(adServerPod(2, ttl, null))).isNull();
        assertThat(target.ask(adServerPod(2, ttl, new TextNode("no-such-granularity")))).isNull();
        assertThat(target.ask(adServerPod(2, ttl, MAPPER.createObjectNode().put("ranges", "x")))).isNull();
    }

    @Test
    public void aPodsPriceIsItsPaidWinnersPrices() {
        final Candidate sim1 = candidate("b0", "amt", bid("a1", "s1", "sim1", "a.example", 12.5));
        final Candidate sim2 = candidate("b1", "amt", bid("a2", "s2", "sim2", "b.example", 22.5));
        final Candidate filler = candidate("b2", "ownbook", house("h1", "s1", "house1"));
        // Only the own book's house flag marks a filler: a third party that sets it still sells its ad.
        final Candidate claimed = candidate("b3", "amt", house("h2", "s2", "house2"));

        assertThat(PodCache.price(List.of(sim1, sim2, filler))).isEqualByComparingTo("35.0");
        assertThat(PodCache.price(List.of(filler))).isEqualByComparingTo("0");
        assertThat(PodCache.price(List.of(claimed))).isEqualByComparingTo("0.01");
        assertThat(PodCache.price(List.of())).isEqualByComparingTo("0");
    }

    @Test
    public void thePodIsCachedOnceAndAnsweredWithItsTargeting() {
        when(cacheService.cachePutObjects(anyList(), any(), anySet(), any(), any(), anyString(), any()))
                .thenReturn(Future.succeededFuture(BidCacheResponse.of(List.of(CacheObject.of("u-1")))));
        when(cacheService.getEndpointHost()).thenReturn("pbs.cdn.example");
        when(cacheService.getEndpointPath()).thenReturn("/cache");
        final PodCache.Ask ask = target.ask(adServerPod(2, MAPPER.getNodeFactory().numberNode(300), CTV_GRANULARITY));

        final PodCache.Cached cached = target.put("<VAST/>", new BigDecimal("35.04"), ask, account, timeout).result();

        assertThat(cached).isEqualTo(new PodCache.Cached("u-1", "35.00", new BigDecimal("35.04")));
        @SuppressWarnings("unchecked") final ArgumentCaptor<List<BidPutObject>> puts = ArgumentCaptor.forClass(List.class);
        verify(cacheService).cachePutObjects(puts.capture(), eq(false), anySet(), eq("fastm"), any(),
                eq("fast-ctv"), eq(timeout));
        assertThat(puts.getValue()).singleElement().satisfies(put -> {
            assertThat(put.getType()).isEqualTo("xml");
            assertThat(put.getValue().asText()).isEqualTo("<VAST/>");
            assertThat(put.getTtlseconds()).isEqualTo(300);
            assertThat(put.getKey()).isNull(); // Prebid Cache generates the uuid
        });

        final ObjectNode answer = target.answer("r", cached, "sid.evid", "s1", 4);
        final JsonNode targeting = answer.at("/seatbid/0/bid/0/ext/prebid/targeting");
        assertThat(answer.at("/seatbid/0/seat").asText()).isEqualTo("fast-ctv");
        assertThat(answer.at("/seatbid/0/bid/0/id").asText()).isEqualTo("sid.evid");
        assertThat(answer.at("/seatbid/0/bid/0/impid").asText()).isEqualTo("s1");
        assertThat(targeting.path("hb_pb").asText()).isEqualTo("35.00");
        assertThat(targeting.path("hb_uuid").asText()).isEqualTo("u-1");
        assertThat(targeting.path("hb_cache_id").asText()).isEqualTo("u-1");
        assertThat(targeting.path("hb_bidder").asText()).isEqualTo("fast-ctv");
        assertThat(targeting.path("hb_cache_host").asText()).isEqualTo("pbs.cdn.example");
        assertThat(targeting.path("hb_cache_path").asText()).isEqualTo("/cache");
        assertThat(answer.at("/ext/fast/ads").asInt()).isEqualTo(4);
    }

    @Test
    public void aPodWithoutPaidAdsIsNeitherCachedNorBid() {
        final PodCache.Ask ask = target.ask(adServerPod(2, MAPPER.getNodeFactory().numberNode(300), CTV_GRANULARITY));

        assertThat(target.put("<VAST/>", BigDecimal.ZERO, ask, account, timeout).result()).isNull();
        // Below the first increment the bucket is 0.00: no line item can match it.
        assertThat(target.put("<VAST/>", new BigDecimal("0.05"), ask, account, timeout).result()).isNull();
        verify(cacheService, never()).cachePutObjects(anyList(), any(), anySet(), any(), any(), anyString(), any());
        assertThat(target.answer("r", null, null, null, 0).toString())
                .isEqualTo("{\"id\":\"r\",\"cur\":\"USD\",\"seatbid\":[]}");
    }

    @Test
    public void aCacheThatAnswersWithoutOneUuidFails() {
        final PodCache.Ask ask = target.ask(adServerPod(2, MAPPER.getNodeFactory().numberNode(300), CTV_GRANULARITY));
        for (BidCacheResponse response : List.of(BidCacheResponse.of(List.of()),
                BidCacheResponse.of(List.of(CacheObject.of("a"), CacheObject.of("b"))),
                BidCacheResponse.of(List.of(CacheObject.of(" "))))) {
            when(cacheService.cachePutObjects(anyList(), any(), anySet(), any(), any(), anyString(), any()))
                    .thenReturn(Future.succeededFuture(response));

            final Future<PodCache.Cached> put = target.put("<VAST/>", BigDecimal.TEN, ask, account, timeout);

            assertThat(put.failed()).isTrue();
        }
    }

    private static Candidate candidate(String ref, String seat, com.iab.openrtb.response.Bid bid) {
        return new PodRenderer().parse(ref, seat, bid);
    }
}
