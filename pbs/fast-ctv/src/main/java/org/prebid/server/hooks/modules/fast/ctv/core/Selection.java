package org.prebid.server.hooks.modules.fast.ctv.core;

import java.util.List;
import java.util.Map;

/**
 * The pod to render: {@code /select}'s answer, or the module's house-only fallback.
 *
 * @param pod        the winners, in slot order, each with its {@code sequence} (1..n)
 * @param standalone per winner, the ad that replaces it in its slot when it fails (ad buffet)
 * @param trackers   extra impression URLs per bid reference; the module's own tracker is always added
 * @param reason     the selection's reason code (logged), or the fallback's
 */
public record Selection(List<Pick> pod, List<Pick> standalone, Map<String, List<String>> trackers, String reason) {

    /**
     * One placed bid.
     *
     * @param impid    the slot (imp id)
     * @param ref      the bid's reference ({@link Candidate#ref()})
     * @param sequence the position in the pod (1..n) for a winner, null for a standalone
     */
    public record Pick(String impid, String ref, Integer sequence) {
    }

    public static Selection empty(String reason) {
        return new Selection(List.of(), List.of(), Map.of(), reason);
    }
}
