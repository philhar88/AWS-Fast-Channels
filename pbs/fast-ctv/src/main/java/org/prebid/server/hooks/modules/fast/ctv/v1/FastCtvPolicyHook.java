package org.prebid.server.hooks.modules.fast.ctv.v1;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.BidRequest;
import io.vertx.core.Future;
import org.prebid.server.hooks.execution.v1.InvocationResultImpl;
import org.prebid.server.hooks.execution.v1.auction.AuctionRequestPayloadImpl;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyClient;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyDecision;
import org.prebid.server.hooks.modules.fast.ctv.core.PolicyPatch;
import org.prebid.server.hooks.modules.fast.ctv.core.Signals;
import org.prebid.server.hooks.v1.InvocationAction;
import org.prebid.server.hooks.v1.InvocationResult;
import org.prebid.server.hooks.v1.InvocationStatus;
import org.prebid.server.hooks.v1.auction.AuctionInvocationContext;
import org.prebid.server.hooks.v1.auction.AuctionRequestPayload;
import org.prebid.server.hooks.v1.auction.ProcessedAuctionRequestHook;
import org.prebid.server.model.HttpRequestContext;

import java.util.Map;
import java.util.Objects;
import java.util.TreeSet;
import java.util.function.Consumer;

/**
 * Asks the Pod Decision Service's {@code /policy} what this break may use, and applies the answer's patch
 * ({@link PolicyPatch}): third-party bidders, regulations, device, content, user data and deals.
 * <p>
 * The stored requests name only the own book. Any failure (non-200, malformed, unknown schema version, timeout,
 * unreachable) changes nothing, so no third party is called without a positive, in-time answer. Prebid Server's group
 * timeout covers a hung or throwing hook the same way, so the account's group timeout sits above
 * {@code policy-timeout-ms}. The outcome is handed to {@link FastCtvVastHook} as module context.
 */
public class FastCtvPolicyHook implements ProcessedAuctionRequestHook {

    public static final String CODE = "fast-ctv-policy";

    private final PolicyClient client;
    private final ObjectMapper mapper;
    private final Consumer<String> log;

    /**
     * @param log receives one JSON line per auction ({@code fast-ctv-request}), or null for none
     */
    public FastCtvPolicyHook(PolicyClient client, ObjectMapper mapper, Consumer<String> log) {
        this.client = Objects.requireNonNull(client);
        this.mapper = Objects.requireNonNull(mapper);
        this.log = log;
    }

    @Override
    public Future<InvocationResult<AuctionRequestPayload>> call(AuctionRequestPayload payload,
                                                                AuctionInvocationContext context) {

        final HttpRequestContext http = context.auctionContext().getHttpRequest();
        final Signals signals = Signals.of(http);
        if (log != null) {
            log.accept(requestLine(http, signals, payload.bidRequest()).toString());
        }
        return client.decide(signals, context.timeout().remaining())
                .map(decision -> result(decision, payload.bidRequest()));
    }

    private InvocationResult<AuctionRequestPayload> result(PolicyDecision decision, BidRequest original) {
        final PolicyPatch patch = decision.patch();
        if (patch == null || !patch.changesRequest()) {
            return unchanged(decision);
        }
        final BidRequest patched;
        try {
            patched = patch.apply(original, mapper);
        } catch (RuntimeException e) {
            return unchanged(new PolicyDecision(null, "POLICY_NOT_APPLICABLE " + e.getClass().getSimpleName(),
                    decision.latencyMs()));
        }
        return InvocationResultImpl.<AuctionRequestPayload>builder()
                .status(InvocationStatus.success)
                .action(InvocationAction.update)
                .payloadUpdate(initial -> AuctionRequestPayloadImpl.of(initial.bidRequest() == original
                        ? patched : patch.apply(initial.bidRequest(), mapper)))
                .moduleContext(decision)
                .build();
    }

    private static InvocationResult<AuctionRequestPayload> unchanged(PolicyDecision decision) {
        return InvocationResultImpl.<AuctionRequestPayload>builder()
                .status(InvocationStatus.success)
                .action(InvocationAction.no_action)
                .message(decision.fallback())
                .moduleContext(decision)
                .build();
    }

    /**
     * The {@code fast-ctv-request} log line: header names and lengths, never a ticket or secret value.
     */
    private ObjectNode requestLine(HttpRequestContext http, Signals signals, BidRequest request) {
        final TreeSet<String> names = new TreeSet<>();
        if (http != null) {
            http.getHeaders().entries().stream().map(Map.Entry::getKey).map(String::toLowerCase).forEach(names::add);
        }
        final ObjectNode line = mapper.createObjectNode().put("evt", "fast-ctv-request");
        line.putPOJO("header_names", names);
        line.put("t_len", signals.ticket().length())
                .put("sid", signals.sid())
                .put("pc", signals.pc())
                .put("fv", signals.fv())
                .put("evid", signals.evid())
                .put("probe", signals.probe())
                .put("ctx_len", signals.ctxLength())
                .put("ua_len", signals.userAgent().length())
                .put("imps", request.getImp().size());
        line.put("dur_ms", signals.durMs());
        line.put("tmax", request.getTmax());
        return line;
    }

    @Override
    public String code() {
        return CODE;
    }
}
