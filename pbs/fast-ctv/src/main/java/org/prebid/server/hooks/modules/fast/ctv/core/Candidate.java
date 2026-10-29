package org.prebid.server.hooks.modules.fast.ctv.core;

import com.iab.openrtb.response.Bid;
import org.w3c.dom.Element;

import java.util.List;
import java.util.Objects;

/**
 * One bid of the auction whose {@code adm} is a usable VAST ad, as {@code /select} sees it and the renderer uses it.
 *
 * @param ref           the bid's reference in the {@code /select} exchange ({@code b0}, {@code b1}, ...)
 * @param seat          the seat (bidder code) that bid
 * @param bid           the bid
 * @param ad            the {@code <Ad>} element of the bid's VAST document (prices substituted)
 * @param kind          whether the ad is inline or a wrapper
 * @param vastDurMs     the inline ad's {@code <Duration>} in milliseconds, or null (wrappers have none)
 * @param hasImpression whether the ad already has an {@code <Impression>} URL
 * @param house         a house ad of the own book ({@code ext.fast.house}): a filler, never sold
 */
public record Candidate(String ref, String seat, Bid bid, Element ad, Kind kind, Integer vastDurMs,
                        boolean hasImpression, boolean house) {

    public enum Kind { INLINE, WRAPPER }

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
