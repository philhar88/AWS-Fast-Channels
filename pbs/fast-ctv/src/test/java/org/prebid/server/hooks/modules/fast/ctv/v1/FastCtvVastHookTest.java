package org.prebid.server.hooks.modules.fast.ctv.v1;

import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.response.BidResponse;
import io.vertx.core.Future;
import io.vertx.core.MultiMap;
import org.junit.jupiter.api.Test;
import org.prebid.server.auction.model.AuctionContext;
import org.prebid.server.execution.timeout.TimeoutFactory;
import org.prebid.server.hooks.execution.v1.exitpoint.ExitpointPayloadImpl;
import org.prebid.server.hooks.modules.fast.ctv.core.HousePod;
import org.prebid.server.hooks.modules.fast.ctv.core.ModuleContext;
import org.prebid.server.hooks.modules.fast.ctv.core.PodShape;
import org.prebid.server.hooks.modules.fast.ctv.core.PodRenderer;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyDecision;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyPatch;
import org.prebid.server.hooks.modules.fast.ctv.core.SelectClient;
import org.prebid.server.hooks.modules.fast.ctv.core.TrackerTemplate;
import org.prebid.server.hooks.v1.InvocationAction;
import org.prebid.server.hooks.v1.InvocationResult;
import org.prebid.server.hooks.v1.auction.AuctionInvocationContext;
import org.prebid.server.hooks.v1.exitpoint.ExitpointPayload;
import org.prebid.server.model.CaseInsensitiveMultiMap;
import org.prebid.server.model.HttpRequestContext;
import org.prebid.server.proto.openrtb.ext.request.ExtRequest;
import org.prebid.server.vertx.httpclient.HttpClient;
import org.prebid.server.vertx.httpclient.model.HttpClientResponse;

import java.time.Clock;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeoutException;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.DECISION;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.EVID;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.SID;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.TICKET;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.TRACKER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.bid;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.house;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.mediaTailorRequest;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pdsTracker;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.policyAnswer;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.response;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.seat;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.selectAnswer;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.vast;

public class FastCtvVastHookTest {

    private static final String OUR_TRACKER = "https://cdn.example/ads/b.gif?d=" + SID + "." + EVID + "&c=";

    private final HttpClient httpClient = mock(HttpClient.class);
    private final List<String> log = new ArrayList<>();
    private final FastCtvVastHook target = new FastCtvVastHook(
            new SelectClient(httpClient, MAPPER, "http://pds.fastm.fast.internal/select", 100), new PodRenderer(),
            new TrackerTemplate(TRACKER), MAPPER, log::add);
    /** The kit's house ads as the configuration renders them (``hooks.fast-ctv.house-ads``). */
    private static final HousePod HOUSE_POD = new HousePod(List.of(
            new HousePod.Ad("kit1", vast("kit1", null), 30, 1920, 1080),
            new HousePod.Ad("kit2", vast("kit2", null), 30, 1920, 1080)));
    private final FastCtvVastHook withHousePod = new FastCtvVastHook(
            new SelectClient(httpClient, MAPPER, "http://pds.fastm.fast.internal/select", 100), new PodRenderer(),
            new TrackerTemplate(TRACKER), MAPPER, log::add, HOUSE_POD);
    private static final PolicyDecision TIMED_OUT = new PolicyDecision(null, "POLICY_UNAVAILABLE TimeoutException", 80);
    private static final PolicyDecision APPLIED = new PolicyDecision(
            PolicyPatch.parse(policyAnswer("CONTEXTUAL", "amt"), DECISION), null, 12);
    private static final String EMPTY_SELECTION =
            "{\"v\":1,\"pod\":[],\"standalone\":[],\"trackers\":{},\"reason\":\"empty\"}";

    /**
     * The bids of {@link org.prebid.server.hooks.modules.fast.ctv.Fixtures#selectAnswer}: house1/house2 from the own
     * book, sim1/sim2 from the simulator.
     */
    private static BidResponse auction() {
        return response(seat("ownbook", house("h1", "s1", "house1"), house("h2", "s2", "house2")),
                seat("amt", bid("a1", "s1", "sim1", "brand-a.example", 12.5),
                        bid("a2", "s2", "sim2", "brand-b.example", 22.5)));
    }

    @Test
    public void thePodIsExactlyWhatSelectReturnsAsXml() {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null, selectAnswer().toString())));

        final InvocationResult<ExitpointPayload> result = call(pod(2), auction());
        final ExitpointPayload payload = result.payloadUpdate().apply(ExitpointPayloadImpl.of(
                MultiMap.caseInsensitiveMultiMap().add("Content-Type", "application/json"), "{}"));

        assertThat(result.action()).isEqualTo(InvocationAction.update);
        assertThat(payload.responseHeaders().get("Content-Type")).isEqualTo("application/xml");
        assertThat(ads(payload.responseBody())).containsExactly("sim1#1", "sim2#2", "house1#", "house2#");
        // The service's tracker where it lists one, the module's own (enr 0) where it lists none: every ad has ours.
        assertThat(payload.responseBody()).contains("<Impression><![CDATA[" + pdsTracker("sim1") + "]]>")
                .contains("<Impression><![CDATA[" + pdsTracker("house1") + "]]>")
                .doesNotContain(OUR_TRACKER + "sim1&e=impression&enr=0")
                .contains("<Impression><![CDATA[" + OUR_TRACKER + "house2&e=impression&enr=0]]>");
        assertThat(log).singleElement().satisfies(line -> assertThat(line)
                .contains("\"winners\":[\"amt:sim1\",\"amt:sim2\"]").doesNotContain(TICKET));
    }

    @Test
    public void buffetOffDropsTheStandalones() {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null, selectAnswer().toString())));

        assertThat(ads(body(call(pod(2), auction(), "X-Fast-Buffet", "0")))).containsExactly("sim1#1", "sim2#2");
    }

    @Test
    public void whenSelectFailsThePodIsTheAuctionsHouseAds() {
        final List<Future<HttpClientResponse>> failures = List.of(
                Future.succeededFuture(HttpClientResponse.of(503, null, "")),
                Future.succeededFuture(HttpClientResponse.of(200, null, selectAnswer().put("reason", "OK").toString())),
                Future.succeededFuture(HttpClientResponse.of(200, null, selectAnswer().put("debug", 1).toString())),
                Future.failedFuture(new TimeoutException("Timeout period of 100ms has been exceeded")));
        for (Future<HttpClientResponse> failure : failures) {
            log.clear();
            selectAnswers(failure);

            final String vast = body(call(pod(2), auction()));

            assertThat(ads(vast)).containsExactly("house1#1", "house2#2");
            assertThat(vast).contains(OUR_TRACKER + "house1&e=impression&enr=0");
            assertThat(log.getFirst()).contains("SELECT_FALLBACK");
        }
    }

    @Test
    public void aBreakWithoutItsIdentifiersIsServedTheHouseFallbackWithoutAsking() {
        final AuctionInvocationContext context = mock(AuctionInvocationContext.class);
        when(context.auctionContext()).thenReturn(AuctionContext.builder()
                .httpRequest(HttpRequestContext.builder().headers(CaseInsensitiveMultiMap.empty())
                        .queryParams(CaseInsensitiveMultiMap.empty()).build())
                .bidRequest(pod(2)).bidResponse(auction()).build());
        when(context.timeout()).thenReturn(new TimeoutFactory(Clock.systemUTC()).create(150));
        when(context.moduleContext()).thenReturn(new PolicyDecision(null, "POLICY_SKIPPED sid", 0));

        final String vast = body(target.call(ExitpointPayloadImpl.of(MultiMap.caseInsensitiveMultiMap(), "{}"),
                context).result());

        assertThat(ads(vast)).containsExactly("house1#1", "house2#2");
        assertThat(log.getFirst()).contains("SELECT_FALLBACK SELECT_SKIPPED");
        verify(httpClient, never()).request(any(), anyString(), any(), anyString(), anyLong(), anyLong());
    }

    @Test
    public void anExceptionRendersAnEmptyVast() {
        final AuctionInvocationContext broken = mock(AuctionInvocationContext.class);
        when(broken.auctionContext()).thenThrow(new IllegalStateException("broken"));

        final InvocationResult<ExitpointPayload> result = target.call(ExitpointPayloadImpl.of(
                MultiMap.caseInsensitiveMultiMap(), "{}"), broken).result();

        assertThat(result.payloadUpdate().apply(null).responseBody()).isEqualTo(PodRenderer.EMPTY_VAST);
    }

    @Test
    public void anEmptySelectionIsAnEmptyVast() {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null,
                "{\"v\":1,\"pod\":[],\"standalone\":[],\"trackers\":{},\"reason\":\"short_avail\"}")));

        assertThat(body(call(pod(1), auction()))).isEqualTo(PodRenderer.EMPTY_VAST);
    }

    @Test
    public void withoutThePolicyAndASilentOwnBookThePodIsTheConfiguredHouseAds() {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null, EMPTY_SELECTION)));

        final String vast = body(call(withHousePod, pod(2), response(), TIMED_OUT));

        assertThat(ads(vast)).containsExactly("kit1#1", "kit2#2");
        assertThat(vast).contains("<Impression><![CDATA[" + OUR_TRACKER + "kit1&e=impression&enr=0]]>");
        assertThat(log).singleElement().satisfies(line -> assertThat(line)
                .contains("\"select\":\"empty HOUSE_CONFIG\"", "\"safe\":true",
                        "\"winners\":[\"fast-ctv:kit1\",\"fast-ctv:kit2\"]",
                        "\"policy_fallback\":\"POLICY_UNAVAILABLE TimeoutException\""));
    }

    @Test
    public void whenSelectFailsAndTheAuctionHasNoHouseAdThePodIsTheConfiguredOnes() {
        selectAnswers(Future.failedFuture(new TimeoutException("Timeout period of 100ms has been exceeded")));

        final String vast = body(call(withHousePod, pod(2), response(seat("ownbook")), APPLIED));

        assertThat(ads(vast)).containsExactly("kit1#1", "kit2#2");
        assertThat(log.getFirst()).contains("SELECT_FALLBACK", "HOUSE_CONFIG", "\"safe\":false");
    }

    @Test
    public void theAuctionsHouseAdsComeBeforeTheConfiguredOnes() {
        selectAnswers(Future.failedFuture(new TimeoutException("Timeout period of 100ms has been exceeded")));

        assertThat(ads(body(call(withHousePod, pod(2), auction(), TIMED_OUT)))).containsExactly("house1#1", "house2#2");
        assertThat(log.getFirst()).contains("SELECT_FALLBACK").doesNotContain("HOUSE_CONFIG");
    }

    @Test
    public void aPodSelectEmptiedWithThePolicyAppliedStaysEmpty() {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null, EMPTY_SELECTION)));

        assertThat(body(call(withHousePod, pod(2), auction(), APPLIED))).isEqualTo(PodRenderer.EMPTY_VAST);
        assertThat(log.getFirst()).contains("\"select\":\"empty\"", "\"safe\":false");
    }

    @Test
    public void withoutConfiguredHouseAdsAPolicylessEmptyPodStaysEmpty() {
        selectAnswers(Future.succeededFuture(HttpClientResponse.of(200, null, EMPTY_SELECTION)));

        assertThat(body(call(target, pod(2), response(), TIMED_OUT))).isEqualTo(PodRenderer.EMPTY_VAST);
    }

    private void selectAnswers(Future<HttpClientResponse> response) {
        when(httpClient.request(any(), anyString(), any(), anyString(), anyLong(), anyLong())).thenReturn(response);
    }

    private InvocationResult<ExitpointPayload> call(BidRequest request, BidResponse response, String... headers) {
        return call(target, request, response, new PolicyDecision(null, "POLICY_HTTP_500", 3), headers);
    }

    private static InvocationResult<ExitpointPayload> call(FastCtvVastHook hook, BidRequest request,
                                                           BidResponse response, PolicyDecision policy,
                                                           String... headers) {
        final AuctionInvocationContext context = mock(AuctionInvocationContext.class);
        when(context.auctionContext()).thenReturn(AuctionContext.builder()
                .httpRequest(mediaTailorRequest(TICKET, headers))
                .bidRequest(request).bidResponse(response).build());
        when(context.timeout()).thenReturn(new TimeoutFactory(Clock.systemUTC()).create(150));
        when(context.moduleContext()).thenReturn(policy);
        return hook.call(ExitpointPayloadImpl.of(MultiMap.caseInsensitiveMultiMap(), "{}"), context).result();
    }

    @Test
    public void aRejectedAuctionIsAnEmptyVastAndALineThatIsNoFallback() {
        final List<String> lines = new ArrayList<>();
        final FastCtvVastHook hook = new FastCtvVastHook(mock(SelectClient.class), new PodRenderer(),
                new TrackerTemplate(TRACKER),
                MAPPER, lines::add, HousePod.parse("[]", MAPPER));
        final PodShape.Result refused = new PodShape(30, 120).apply(BidRequest.builder().id("r")
                .ext(rejectedExt()).imp(List.of()).build());
        final AuctionInvocationContext context = mock(AuctionInvocationContext.class);
        when(context.auctionContext()).thenReturn(AuctionContext.builder()
                .httpRequest(mediaTailorRequest(TICKET))
                .bidRequest(refused.request()).bidResponse(BidResponse.builder().seatbid(List.of()).build())
                .requestRejected(true).build());
        when(context.timeout()).thenReturn(new TimeoutFactory(Clock.systemUTC()).create(150));
        when(context.moduleContext()).thenReturn(new ModuleContext(null, refused));

        final InvocationResult<ExitpointPayload> result =
                hook.call(ExitpointPayloadImpl.of(MultiMap.caseInsensitiveMultiMap(), "{}"), context).result();

        assertThat(body(result)).isEqualTo(PodRenderer.EMPTY_VAST);
        assertThat(lines).singleElement().satisfies(line -> {
            assertThat(line).contains("\"select\":\"REJECTED UNREADABLE\"")
                    .doesNotContain("POLICY_").doesNotContain("SELECT_FALLBACK");
            assertThat(line).contains("pod_s is not whole seconds");
        });
    }

    private static ExtRequest rejectedExt() {
        final ExtRequest ext = ExtRequest.of(null);
        ext.addProperty(PodShape.EXT,
                MAPPER.createObjectNode().put(PodShape.SLOT_FIELD, 30).put(PodShape.POD_FIELD, ""));
        return ext;
    }

    private static String body(InvocationResult<ExitpointPayload> result) {
        return result.payloadUpdate().apply(null).responseBody();
    }

    private static List<String> ads(String vast) {
        final Matcher matcher = Pattern.compile("<Ad id=\"[^\"]*\"( sequence=\"(\\d+)\")?>.*?<AdTitle>([^<]+)</AdTitle>")
                .matcher(vast);
        final List<String> ads = new ArrayList<>();
        while (matcher.find()) {
            ads.add(matcher.group(3) + "#" + (matcher.group(2) != null ? matcher.group(2) : ""));
        }
        return ads;
    }
}
