package org.prebid.server.hooks.modules.fast.ctv.core;

import com.iab.openrtb.response.Bid;
import org.w3c.dom.Element;

import java.util.List;
import java.util.Objects;

/**
 * One bid of the auction, as {@code /select} sees it and the renderer uses it. Every bid for an imp of the request is
 * a candidate, so the Pod Decision Service records it; only one whose {@code adm} is a usable VAST ad
 * ({@link #usable()}) can be placed.
 *
 * @param ref           the bid's reference in the {@code /select} exchange ({@code b0}, {@code b1}, ...)
 * @param seat          the seat (bidder code) that bid
 * @param bid           the bid
 * @param ad            the {@code <Ad>} element of the bid's VAST document (prices substituted), or null when the
 *                      {@code adm} is not usable VAST
 * @param kind          whether the ad is inline or a wrapper, or null when it is not usable
 * @param vastDurMs     the inline ad's {@code <Duration>} in milliseconds, or null (wrappers have none)
 * @param hasImpression whether the ad already has an {@code <Impression>} URL
 * @param house         a house ad of the own book ({@code ext.fast.house}): a filler, never sold
 */
public record Candidate(String ref, String seat, Bid bid, Element ad, Kind kind, Integer vastDurMs,
                        boolean hasImpression, boolean house) {

    public enum Kind { INLINE, WRAPPER }

    /**
     * A bid whose {@code adm} is not a usable VAST ad: sent to {@code /select} (with {@code adm_kind} null), never
     * rendered.
     */
    public static Candidate unusable(String ref, String seat, Bid bid, boolean house) {
        return new Candidate(ref, seat, bid, null, null, null, false, house);
    }

    public boolean usable() {
        return ad != null;
    }

    public String impid() {
        return bid.getImpid();
    }

    public String crid() {
        return Objects.requireNonNullElse(bid.getCrid(), "");
    }

    public List<String> adomain() {
        return bid.getAdomain() != null ? bid.getAdomain() : List.of();
    }
}
