package org.prebid.server.hooks.modules.fast.ctv.v1;

import com.iab.openrtb.request.BidRequest;
import io.vertx.core.Future;
import io.vertx.core.http.HttpMethod;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.prebid.server.auction.model.AuctionContext;
import org.prebid.server.execution.timeout.TimeoutFactory;
import org.prebid.server.hooks.execution.v1.auction.AuctionRequestPayloadImpl;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyClient;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyDecision;
import org.prebid.server.hooks.v1.InvocationAction;
import org.prebid.server.hooks.v1.InvocationResult;
import org.prebid.server.hooks.v1.InvocationStatus;
import org.prebid.server.hooks.v1.auction.AuctionInvocationContext;
import org.prebid.server.hooks.v1.auction.AuctionRequestPayload;
import org.prebid.server.model.HttpRequestContext;
import org.prebid.server.vertx.httpclient.HttpClient;
import org.prebid.server.vertx.httpclient.model.HttpClientResponse;

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
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.TICKET;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.mediaTailorRequest;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pds;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;

public class FastCtvPolicyHookTest {

    private static final String POLICY_URL = "http://pds.fastm.fast.internal/policy";

    private final HttpClient httpClient = mock(HttpClient.class);
    private final List<String> log = new ArrayList<>();
    private final FastCtvPolicyHook target = new FastCtvPolicyHook(
            new PolicyClient(httpClient, MAPPER, POLICY_URL, 50), MAPPER, log::add);

    @Test
    public void aPositiveAnswerAddsItsBiddersAndHandsTheDecisionOn() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(200, null, pds("policy-response-contextual.json")
                .toString())));
        final BidRequest stored = pod(2);

        final InvocationResult<AuctionRequestPayload> result = call(stored, TICKET);

        assertThat(result.status()).isEqualTo(InvocationStatus.success);
        assertThat(result.action()).isEqualTo(InvocationAction.update);
        final BidRequest patched = result.payloadUpdate().apply(AuctionRequestPayloadImpl.of(stored)).bidRequest();
        assertThat(patched.getImp()).allSatisfy(imp ->
                assertThat(imp.getExt().path("prebid").path("bidder").has("amt")).isTrue());
        assertThat(((PolicyDecision) result.moduleContext()).patch().mode().name()).isEqualTo("CONTEXTUAL");
    }

    @Test
    public void theQuestionCarriesTheBreakAndTheSessionAsTheContractSays() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(200, null, pds("policy-response-safe.json").toString())));

        call(pod(1), "");

        final ArgumentCaptor<String> body = ArgumentCaptor.forClass(String.class);
        verify(httpClient).request(eq(HttpMethod.POST), eq(POLICY_URL), any(), body.capture(), anyLong(), anyLong());
        assertThat(MAPPER.readTree(body.getValue())).isEqualTo(pds("policy-request.json"));
    }

    @Test
    public void everyFailureLeavesTheRequestAsStored() throws Exception {
        final List<Future<HttpClientResponse>> failures = List.of(
                Future.succeededFuture(HttpClientResponse.of(500, null, "{}")),
                Future.succeededFuture(HttpClientResponse.of(200, null, "not json")),
                Future.succeededFuture(HttpClientResponse.of(200, null, "{\"v\":2,\"mode\":\"FULL\"}")),
                Future.failedFuture(new TimeoutException("Timeout period of 50ms has been exceeded")),
                Future.failedFuture(new java.net.ConnectException("Connection refused")));
        final List<String> reasons = new ArrayList<>();
        for (Future<HttpClientResponse> failure : failures) {
            answer(failure);

            final InvocationResult<AuctionRequestPayload> result = call(pod(2), TICKET);

            assertThat(result.action()).isEqualTo(InvocationAction.no_action);
            assertThat(result.payloadUpdate()).isNull();
            assertThat(((PolicyDecision) result.moduleContext()).isFallback()).isTrue();
            reasons.add(result.message().split(" ")[0]);
        }
        assertThat(reasons).containsExactly("POLICY_HTTP_500", "POLICY_MALFORMED", "POLICY_MALFORMED",
                "POLICY_UNAVAILABLE", "POLICY_UNAVAILABLE");
    }

    @Test
    public void theLogNamesHeadersButNeverTheTicketOrTheSecret() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(200, null, pds("policy-response-contextual.json")
                .toString())));

        call(pod(2), TICKET);

        assertThat(log).hasSize(1);
        final String line = log.getFirst();
        assertThat(MAPPER.readTree(line).path("t_len").asInt()).isEqualTo(TICKET.length());
        assertThat(MAPPER.readTree(line).path("header_names").toString()).contains("x-fast-origin", "x-fast-t");
        assertThat(line).doesNotContain(TICKET).doesNotContain("origin-secret-value");
    }

    @Test
    public void aMalformedTicketIsNeverForwarded() throws Exception {
        answer(Future.succeededFuture(HttpClientResponse.of(500, null, "")));

        call(pod(1), "\"ticket\\with quotes");

        final ArgumentCaptor<String> body = ArgumentCaptor.forClass(String.class);
        verify(httpClient).request(any(), anyString(), any(), body.capture(), anyLong(), anyLong());
        assertThat(MAPPER.readTree(body.getValue()).path("t").asText()).isEmpty();
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
