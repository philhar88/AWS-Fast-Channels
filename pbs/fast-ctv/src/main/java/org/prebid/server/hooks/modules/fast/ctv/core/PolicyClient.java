package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.vertx.core.Future;
import io.vertx.core.http.HttpMethod;
import org.prebid.server.util.HttpUtil;
import org.prebid.server.vertx.httpclient.HttpClient;
import org.prebid.server.vertx.httpclient.model.HttpClientResponse;

import java.util.Objects;

/**
 * Asks the Pod Decision Service's {@code /policy} what one break may use ({@code contracts/pds-policy.schema.json},
 * version 1).
 * <p>
 * Every failure becomes a fallback with a reason, never an exception: a break the request cannot identify (no call
 * is made, {@code POLICY_SKIPPED <field>}), a non-200 answer, a body that is not JSON or not the schema, a timeout or
 * an unreachable service. The caller then leaves the request as stored.
 */
public class PolicyClient {

    static final long MAX_RESPONSE_BYTES = 64 * 1024;

    private final HttpClient httpClient;
    private final ObjectMapper mapper;
    private final String url;
    private final long timeoutMs;

    public PolicyClient(HttpClient httpClient, ObjectMapper mapper, String url, long timeoutMs) {
        this.httpClient = Objects.requireNonNull(httpClient);
        this.mapper = Objects.requireNonNull(mapper);
        this.url = Urls.require(url, "hooks.fast-ctv.policy-url");
        this.timeoutMs = timeoutMs;
    }

    /**
     * The policy for the break {@code signals} describe, within {@code min(policy timeout, remainingMs)}.
     */
    public Future<PolicyDecision> decide(Signals signals, long remainingMs) {
        final long started = System.nanoTime();
        final String problem = signals.problem();
        if (problem != null) {
            return Future.succeededFuture(PolicyDecision.fallback("POLICY_SKIPPED " + problem, started));
        }
        final long timeout = Math.max(1, Math.min(timeoutMs, remainingMs));
        return httpClient.request(HttpMethod.POST, url, HttpUtil.headers(), question(signals).toString(), timeout,
                        MAX_RESPONSE_BYTES)
                .map(response -> read(response, signals.decisionId(), started))
                .otherwise(error -> PolicyDecision.fallback(
                        "POLICY_UNAVAILABLE " + error.getClass().getSimpleName(), started));
    }

    /**
     * The {@code /policy} request body (every field of the schema, none other).
     */
    public ObjectNode question(Signals signals) {
        final ObjectNode question = mapper.createObjectNode()
                .put("v", PolicyPatch.VERSION)
                .put("t", signals.ticket())
                .put("sid", signals.sid())
                .put("pc", signals.pc())
                .put("fv", signals.fv())
                .put("evid", signals.evid())
                .put("upid", signals.upid());
        question.put("dur_ms", signals.durMs());
        return question.put("ua", signals.userAgent())
                .put("xff", signals.xff())
                .put("cfva", signals.cfva())
                .put("ctx", signals.ctx())
                .put("probe", signals.probe());
    }

    private PolicyDecision read(HttpClientResponse response, String decisionId, long started) {
        if (response.getStatusCode() != 200) {
            return PolicyDecision.fallback("POLICY_HTTP_" + response.getStatusCode(), started);
        }
        try {
            return PolicyDecision.of(PolicyPatch.parse(mapper.readTree(response.getBody()), decisionId), started);
        } catch (Exception e) {
            return PolicyDecision.fallback("POLICY_MALFORMED " + e.getMessage(), started);
        }
    }
}
