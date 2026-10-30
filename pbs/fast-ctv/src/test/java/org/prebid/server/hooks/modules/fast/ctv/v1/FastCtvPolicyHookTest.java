package org.prebid.server.hooks.modules.fast.ctv.v1;

import com.iab.openrtb.request.BidRequest;
import io.vertx.core.Future;
import io.vertx.core.http.HttpMethod;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.prebid.server.auction.model.AuctionContext;
import org.prebid.server.execution.timeout.TimeoutFactory;
import org.prebid.server.hooks.execution.v1.auction.AuctionRequestPayloadImpl;
import org.prebid.server.hooks.modules.fast.ctv.core.ModuleContext;
import org.prebid.server.hooks.modules.fast.ctv.core.PodShape;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyClient;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyDecision;
import org.prebid.server.hooks.v1.InvocationAction;
import org.prebid.server.hooks.v1.InvocationResult;
import org.prebid.server.hooks.v1.InvocationStatus;
import org.prebid.server.hooks.v1.auction.AuctionInvocationContext;
import org.prebid.server.hooks.v1.auction.AuctionRequestPayload;
import org.prebid.server.model.CaseInsensitiveMultiMap;
import org.prebid.server.model.HttpRequestContext;
import org.prebid.server.vertx.httpclient.HttpClient;
import org.prebid.server.vertx.httpclient.model.HttpClientResponse;

import org.prebid.server.proto.openrtb.ext.request.ExtRequest;
import org.prebid.server.proto.openrtb.ext.request.ExtRequestPrebid;

import java.time.Clock;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeoutException;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.TICKET;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.mediaTailorRequest;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.policyAnswer;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;

public class FastCtvPolicyHookTest {

    private static final String POLICY_URL = "http://pds.fastm.fast.internal/policy";

    private final HttpClient httpClient = mock(HttpClient.class);
    private final List<String> log = new ArrayList<>();
    private final FastCtvPolicyHook target = new FastCtvPolicyHook(
            new PolicyClient(httpClient, MAPPER, POLICY_URL, 50), MAPPER, log::add);

    @Test
    public void aPositiveAnswerAddsItsBiddersAndHandsTheDecisionOn() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(200, null, policyAnswer("CONTEXTUAL", "amt").toString())));
        final BidRequest stored = pod(2);

        final InvocationResult<AuctionRequestPayload> result = call(stored, TICKET);

        assertThat(result.status()).isEqualTo(InvocationStatus.success);
        assertThat(result.action()).isEqualTo(InvocationAction.update);
        final BidRequest patched = result.payloadUpdate().apply(AuctionRequestPayloadImpl.of(stored)).bidRequest();
        assertThat(patched.getImp()).allSatisfy(imp ->
                assertThat(imp.getExt().path("prebid").path("bidder").has("amt")).isTrue());
        assertThat(((ModuleContext) result.moduleContext()).policy().patch().mode().name()).isEqualTo("CONTEXTUAL");
    }

    @Test
    public void theQuestionCarriesTheBreakAndTheSessionAsTheContractSays() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(200, null, policyAnswer("SAFE").toString())));

        call(pod(1), "");

        final ArgumentCaptor<String> body = ArgumentCaptor.forClass(String.class);
        verify(httpClient).request(eq(HttpMethod.POST), eq(POLICY_URL), any(), body.capture(), anyLong(), anyLong());
        final List<String> fields = new ArrayList<>();
        MAPPER.readTree(body.getValue()).fieldNames().forEachRemaining(fields::add);
        assertThat(fields).containsExactly("v", "t", "sid", "pc", "fv", "evid", "upid", "dur_ms", "ua", "xff", "cfva",
                "ctx", "probe");
        assertThat(MAPPER.readTree(body.getValue()).path("dur_ms").asInt()).isEqualTo(60060);
    }

    @Test
    public void everyFailureLeavesTheRequestAsStored() throws Exception {
        final List<Future<HttpClientResponse>> failures = List.of(
                Future.succeededFuture(HttpClientResponse.of(500, null, "{}")),
                Future.succeededFuture(HttpClientResponse.of(200, null, "not json")),
                Future.succeededFuture(HttpClientResponse.of(200, null, "{\"v\":2,\"mode\":\"FULL\"}")),
                Future.succeededFuture(HttpClientResponse.of(200, null, policyAnswer("FULL", "amt")
                        .put("decision_id", "another-session.820461826").toString())),
                Future.failedFuture(new TimeoutException("Timeout period of 50ms has been exceeded")),
                Future.failedFuture(new java.net.ConnectException("Connection refused")));
        final List<String> reasons = new ArrayList<>();
        for (Future<HttpClientResponse> failure : failures) {
            answer(failure);

            final InvocationResult<AuctionRequestPayload> result = call(pod(2), TICKET);

            assertThat(result.action()).isEqualTo(InvocationAction.no_action);
            assertThat(result.payloadUpdate()).isNull();
            assertThat(((ModuleContext) result.moduleContext()).policy().isFallback()).isTrue();
            reasons.add(result.message().split(" ")[0]);
        }
        assertThat(reasons).containsExactly("POLICY_HTTP_500", "POLICY_MALFORMED", "POLICY_MALFORMED",
                "POLICY_MALFORMED", "POLICY_UNAVAILABLE", "POLICY_UNAVAILABLE");
    }

    @Test
    public void aBreakWithoutItsIdentifiersIsNeverAskedAbout() {
        final BidRequest stored = pod(1);
        final AuctionInvocationContext context = mock(AuctionInvocationContext.class);
        when(context.auctionContext()).thenReturn(AuctionContext.builder().httpRequest(HttpRequestContext.builder()
                .headers(CaseInsensitiveMultiMap.empty()).queryParams(CaseInsensitiveMultiMap.empty()).build())
                .bidRequest(stored).build());
        when(context.timeout()).thenReturn(new TimeoutFactory(Clock.systemUTC()).create(500));

        final InvocationResult<AuctionRequestPayload> result = target.call(AuctionRequestPayloadImpl.of(stored),
                context).result();

        assertThat(result.action()).isEqualTo(InvocationAction.no_action);
        assertThat(result.message()).isEqualTo("POLICY_SKIPPED sid");
        verify(httpClient, never()).request(any(), anyString(), any(), anyString(), anyLong(), anyLong());
    }

    @Test
    public void theLogNamesHeadersButNeverTheTicketOrTheSecret() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(200, null, policyAnswer("CONTEXTUAL", "amt").toString())));

        call(pod(2), TICKET);

        assertThat(log).hasSize(1);
        final String line = log.getFirst();
        assertThat(MAPPER.readTree(line).path("t_len").asInt()).isEqualTo(TICKET.length());
        assertThat(MAPPER.readTree(line).path("header_names").toString()).contains("x-fast-origin", "x-fast-t");
        assertThat(line).doesNotContain(TICKET).doesNotContain("origin-secret-value");
    }

    @Test
    public void aMalformedTicketIsForwardedOnlyAsItsUrlSafeCharacters() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(500, null, "")));

        call(pod(1), "\"ticket\\with quotes");

        final ArgumentCaptor<String> body = ArgumentCaptor.forClass(String.class);
        verify(httpClient).request(any(), anyString(), any(), body.capture(), anyLong(), anyLong());
        assertThat(MAPPER.readTree(body.getValue()).path("t").asText()).isEqualTo("ticketwithquotes");
        assertThat(body.getValue()).doesNotContain("with quotes");
    }

    @Test
    public void theStoredPodIsShapedToTheBreakBeforeThePatchAndExtFastNeverReachesABidder() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(200, null, policyAnswer("CONTEXTUAL", "amt").toString())));
        final BidRequest stored = storedPod("90");

        final InvocationResult<AuctionRequestPayload> result = call(stored, TICKET);

        assertThat(result.action()).isEqualTo(InvocationAction.update);
        final BidRequest auctioned = result.payloadUpdate().apply(AuctionRequestPayloadImpl.of(stored)).bidRequest();
        assertThat(auctioned.getImp()).hasSize(3).allSatisfy(imp -> {
            assertThat(imp.getVideo().getPoddur()).isEqualTo(90);
            assertThat(imp.getExt().at("/prebid/bidder/amt").isMissingNode()).isFalse();
        });
        assertThat(auctioned.getExt().getProperty(PodShape.EXT)).isNull();
        final ModuleContext state = (ModuleContext) result.moduleContext();
        assertThat(state.shape().podSeconds()).isEqualTo(90);
        assertThat(state.shape().fast().get(PodShape.POD_FIELD).textValue()).isEqualTo("90");
        assertThat(MAPPER.readTree(log.getFirst()).path("pod_s").asInt()).isEqualTo(90);
        assertThat(MAPPER.readTree(log.getFirst()).path("slots").asInt()).isEqualTo(3);
    }

    @Test
    public void withoutAPatchTheShapedPodStillReplacesTheTemplate() {
        answer(Future.failedFuture(new TimeoutException()));
        final BidRequest stored = storedPod("60");

        final InvocationResult<AuctionRequestPayload> result = call(stored, TICKET);

        assertThat(result.action()).isEqualTo(InvocationAction.update);
        assertThat(result.payloadUpdate().apply(AuctionRequestPayloadImpl.of(stored)).bidRequest().getImp())
                .hasSize(2);
        assertThat(((ModuleContext) result.moduleContext()).policy().isFallback()).isTrue();
    }

    @Test
    public void aBreakWithoutAUsableLengthIsRejectedWithoutAskingThePolicy() throws Exception {
        final InvocationResult<AuctionRequestPayload> result = call(storedPod("29"), TICKET);

        assertThat(result.status()).isEqualTo(InvocationStatus.success);
        assertThat(result.action()).isEqualTo(InvocationAction.reject);
        assertThat(((ModuleContext) result.moduleContext()).refused()).isTrue();
        verify(httpClient, never()).request(any(), anyString(), any(), anyString(), anyLong(), anyLong());
        assertThat(MAPPER.readTree(log.getFirst()).path("refused").asText()).isEqualTo("pod_s 29 outside 30-120 s");
    }

    /** The one stored request, merged with MediaTailor's body: a template slot, {@code slot_s} and {@code pod_s}. */
    private static BidRequest storedPod(String podSeconds) {
        final BidRequest template = pod(1);
        final ExtRequest ext = ExtRequest.of(ExtRequestPrebid.builder().build());
        ext.addProperty(PodShape.EXT, MAPPER.createObjectNode().put(PodShape.SLOT_FIELD, 30)
                .put(PodShape.POD_FIELD, podSeconds));
        return template.toBuilder().ext(ext).build();
    }

    private void answer(Future<HttpClientResponse> response) {
        when(httpClient.request(any(), anyString(), any(), anyString(), anyLong(), anyLong())).thenReturn(response);
    }

    private InvocationResult<AuctionRequestPayload> call(BidRequest request, String ticket) {
        final HttpRequestContext http = mediaTailorRequest(ticket);
        final AuctionInvocationContext context = mock(AuctionInvocationContext.class);
        when(context.auctionContext()).thenReturn(AuctionContext.builder().httpRequest(http).bidRequest(request).build());
        when(context.timeout()).thenReturn(new TimeoutFactory(Clock.systemUTC()).create(500));
        return target.call(AuctionRequestPayloadImpl.of(request), context).result();
    }
}
