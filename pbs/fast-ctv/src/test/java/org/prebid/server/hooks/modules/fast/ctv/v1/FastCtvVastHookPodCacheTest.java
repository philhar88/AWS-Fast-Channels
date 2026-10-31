package org.prebid.server.hooks.modules.fast.ctv.v1;

import com.fasterxml.jackson.databind.JsonNode;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.response.BidResponse;
import io.vertx.core.Future;
import io.vertx.core.MultiMap;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.prebid.server.auction.model.AuctionContext;
import org.prebid.server.cache.CoreCacheService;
import org.prebid.server.cache.proto.request.bid.BidPutObject;
import org.prebid.server.cache.proto.response.bid.BidCacheResponse;
import org.prebid.server.cache.proto.response.bid.CacheObject;
import org.prebid.server.execution.timeout.TimeoutFactory;
import org.prebid.server.hooks.execution.v1.exitpoint.ExitpointPayloadImpl;
import org.prebid.server.hooks.modules.fast.ctv.core.HousePod;
import org.prebid.server.hooks.modules.fast.ctv.core.PodCache;
import org.prebid.server.hooks.modules.fast.ctv.core.PodCacheTest;
import org.prebid.server.hooks.modules.fast.ctv.core.PodRenderer;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyDecision;
import org.prebid.server.hooks.modules.fast.ctv.core.SelectClient;
import org.prebid.server.hooks.modules.fast.ctv.core.TrackerTemplate;
import org.prebid.server.hooks.v1.InvocationResult;
import org.prebid.server.hooks.v1.auction.AuctionInvocationContext;
import org.prebid.server.hooks.v1.exitpoint.ExitpointPayload;
import org.prebid.server.settings.model.Account;
import org.prebid.server.vertx.httpclient.HttpClient;
import org.prebid.server.vertx.httpclient.model.HttpClientResponse;

import java.io.IOException;
import java.time.Clock;
import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyList;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anySet;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.DECISION;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.TICKET;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.TRACKER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.bid;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.house;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.mediaTailorRequest;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pdsTracker;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.response;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.seat;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.selectAnswer;

/**
 * The ad-server mode of {@link FastCtvVastHook}: the pod {@code /select} returns is rendered exactly as in the VAST
 * mode, stored in Prebid Cache once, and answered as its Prebid targeting.
 */
public class FastCtvVastHookPodCacheTest {

    private final HttpClient httpClient = mock(HttpClient.class);
    private final CoreCacheService cacheService = mock(CoreCacheService.class);
    private final List<String> log = new ArrayList<>();
    private final FastCtvVastHook target = new FastCtvVastHook(
            new SelectClient(httpClient, MAPPER, "http://127.0.0.1:8081/select", 100), new PodRenderer(),
            new TrackerTemplate(TRACKER), MAPPER, log::add, HousePod.NONE, new PodCache(cacheService, MAPPER));

    private static BidRequest adServerPod() {
        return PodCacheTest.adServerPod(2, MAPPER.getNodeFactory().numberNode(300), PodCacheTest.CTV_GRANULARITY);
    }

    private static BidResponse auction() {
        return response(seat("ownbook", house("h1", "s1", "house1"), house("h2", "s2", "house2")),
                seat("amt", bid("a1", "s1", "sim1", "brand-a.example", 12.5),
                        bid("a2", "s2", "sim2", "brand-b.example", 22.5)));
    }

    @Test
    public void theSelectedPodIsCachedAsVastAndAnsweredAsTargeting() throws IOException {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null, selectAnswer().toString())));
        cacheAnswers(Future.succeededFuture(BidCacheResponse.of(List.of(CacheObject.of("0b9a7c3e-1111-4222-8333-444455556666")))));

        final ExitpointPayload payload = call(adServerPod(), auction());

        assertThat(payload.responseHeaders().get("Content-Type")).isEqualTo("application/json");
        final JsonNode answer = MAPPER.readTree(payload.responseBody());
        final JsonNode targeting = answer.at("/seatbid/0/bid/0/ext/prebid/targeting");
        assertThat(targeting.path("hb_pb").asText()).isEqualTo("35.00"); // 12.5 + 22.5; the house standalones are free
        assertThat(targeting.path("hb_uuid").asText()).isEqualTo("0b9a7c3e-1111-4222-8333-444455556666");
        assertThat(targeting.path("hb_bidder").asText()).isEqualTo("fast-ctv");
        assertThat(targeting.path("hb_cache_host").asText()).isEqualTo("pbs.cdn.example");
        assertThat(answer.at("/seatbid/0/bid/0/id").asText()).isEqualTo(DECISION);
        assertThat(answer.at("/ext/fast/ads").asInt()).isEqualTo(4);

        // The cached document is the pod the VAST mode would have answered: winners in order, standalones, trackers.
        @SuppressWarnings("unchecked") final ArgumentCaptor<List<BidPutObject>> puts = ArgumentCaptor.forClass(List.class);
        verify(cacheService).cachePutObjects(puts.capture(), any(), anySet(), any(), any(), anyString(), any());
        final String vast = puts.getValue().getFirst().getValue().asText();
        assertThat(vast).startsWith("<VAST version=\"4.2\"")
                .contains("sequence=\"1\"").contains("sequence=\"2\"")
                .contains("<Impression><![CDATA[" + pdsTracker("sim1") + "]]>");
        assertThat(log).singleElement().satisfies(line -> assertThat(line)
                .contains("\"cached\":true").contains("\"hb_pb\":\"35.00\"").contains("\"uuid8\":\"0b9a7c3e\"")
                .doesNotContain("4222-8333").doesNotContain(TICKET));
    }

    @Test
    public void aHouseOnlyPodIsNoBidAndIsNotCached() throws IOException {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(503, null, ""))); // SELECT_FALLBACK: house only

        final ExitpointPayload payload = call(adServerPod(), auction());

        assertThat(MAPPER.readTree(payload.responseBody()).path("seatbid").isEmpty()).isTrue();
        verify(cacheService, never()).cachePutObjects(anyList(), any(), anySet(), any(), any(), anyString(), any());
        assertThat(log.getFirst()).contains("\"cached\":false").contains("SELECT_FALLBACK");
    }

    @Test
    public void aFailedCacheWriteIsNoBid() throws IOException {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null, selectAnswer().toString())));
        cacheAnswers(Future.failedFuture(new java.util.concurrent.TimeoutException("Timeout")));

        final ExitpointPayload payload = call(adServerPod(), auction());

        assertThat(payload.responseHeaders().get("Content-Type")).isEqualTo("application/json");
        assertThat(MAPPER.readTree(payload.responseBody()).path("seatbid").isEmpty()).isTrue();
        assertThat(log.getFirst()).contains("\"cached\":false").contains("\"cache_error\":\"TimeoutException\"");
    }

    @Test
    public void anExceptionInTheModeIsNoBidJson() throws IOException {
        final AuctionInvocationContext context = context(adServerPod(), auction());
        when(context.moduleContext()).thenThrow(new IllegalStateException("broken"));

        final ExitpointPayload payload = target.call(ExitpointPayloadImpl.of(MultiMap.caseInsensitiveMultiMap(),
                "{}"), context).result().payloadUpdate().apply(null);

        assertThat(payload.responseHeaders().get("Content-Type")).isEqualTo("application/json");
        assertThat(MAPPER.readTree(payload.responseBody()).path("id").asText()).isEqualTo("r");
        assertThat(MAPPER.readTree(payload.responseBody()).path("seatbid").isEmpty()).isTrue();
    }

    @Test
    public void withoutTheAskThePodIsVastAsBefore() {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null, selectAnswer().toString())));

        final ExitpointPayload payload = call(pod(2), auction());

        assertThat(payload.responseHeaders().get("Content-Type")).isEqualTo("application/xml");
        verify(cacheService, never()).cachePutObjects(anyList(), any(), anySet(), any(), any(), anyString(), any());
    }

    private void selectAnswers(Future<HttpClientResponse> response) {
        when(httpClient.request(any(), anyString(), any(), anyString(), anyLong(), anyLong())).thenReturn(response);
    }

    private void cacheAnswers(Future<BidCacheResponse> response) {
        when(cacheService.cachePutObjects(anyList(), any(), anySet(), any(), any(), anyString(), any()))
                .thenReturn(response);
        when(cacheService.getEndpointHost()).thenReturn("pbs.cdn.example");
        when(cacheService.getEndpointPath()).thenReturn("/cache");
    }

    private AuctionInvocationContext context(BidRequest request, BidResponse response) {
        final AuctionInvocationContext context = mock(AuctionInvocationContext.class);
        when(context.auctionContext()).thenReturn(AuctionContext.builder()
                .httpRequest(mediaTailorRequest(TICKET))
                .account(Account.empty("fastm"))
                .bidRequest(request).bidResponse(response).build());
        when(context.timeout()).thenReturn(new TimeoutFactory(Clock.systemUTC()).create(250));
        when(context.moduleContext()).thenReturn(new PolicyDecision(null, "POLICY_HTTP_500", 3));
        return context;
    }

    private ExitpointPayload call(BidRequest request, BidResponse response) {
        final InvocationResult<ExitpointPayload> result = target.call(
                ExitpointPayloadImpl.of(MultiMap.caseInsensitiveMultiMap(), "{}"), context(request, response))
                .result();
        return result.payloadUpdate().apply(null);
    }
}
