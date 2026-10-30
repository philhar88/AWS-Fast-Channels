package org.prebid.server.hooks.modules.fast.ctv.core;

/**
 * What the policy hook hands the VAST hook: the {@code /policy} outcome and the pod's shape ({@link PodShape}),
 * including the stored request's {@code ext.fast} that shaping removed from the request (the ad-server mode's ask).
 *
 * @param policy the policy outcome, or null when the pod was refused before asking
 * @param shape  the shaping result
 */
public record ModuleContext(PolicyDecision policy, PodShape.Result shape) {

    /** Whether the pod was refused (the auction was rejected). */
    public boolean refused() {
        return shape != null && shape.refused() != null;
    }
}
