package org.prebid.server.hooks.modules.fast.ctv.core;

/**
 * The outcome of one {@code /policy} call, handed from the policy hook to the exitpoint hook as module context:
 * either a patch, or the reason there is none (the request then stays as stored).
 *
 * @param patch     the applied patch, or null on a fallback
 * @param fallback  why there is no patch ({@code POLICY_HTTP_500}, {@code POLICY_UNAVAILABLE TimeoutException},
 *                  ...), or null
 * @param latencyMs how long the call took
 */
public record PolicyDecision(PolicyPatch patch, String fallback, long latencyMs) {

    static PolicyDecision of(PolicyPatch patch, long startedNanos) {
        return new PolicyDecision(patch, null, millisSince(startedNanos));
    }

    static PolicyDecision fallback(String reason, long startedNanos) {
        return new PolicyDecision(null, reason, millisSince(startedNanos));
    }

    public boolean isFallback() {
        return patch == null;
    }

    static long millisSince(long startedNanos) {
        return (System.nanoTime() - startedNanos) / 1_000_000;
    }
}
