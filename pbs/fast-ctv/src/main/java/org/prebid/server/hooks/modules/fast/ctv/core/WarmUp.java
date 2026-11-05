package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Imp;

import java.util.HashSet;
import java.util.Set;

/**
 * The warm-up's stored request ({@code fast-warm}, marked {@code ext.fast.warm}, W-12): its slot names the configured
 * third-party adapters and answers each from a stored bid response ({@code imp.ext.prebid.storedbidresponse}), so a
 * warm-up creates every adapter's request and metric keys without calling it. Prebid Server Java creates an adapter's
 * per-request-type metric key without a lock, and the first concurrent auctions that reach a new key can fail with an
 * HTTP 500; the warm-up creates the keys first.
 * <p>
 * A warm request never gains a bidder from {@code /policy}, and keeps only the own book and the bidders its slots
 * answer from stored responses: a bidder without one would get a real, synthetic bid request.
 */
// OPTIMIZATION(oss): W-12 PBS-Java races creating an adapter's metric keys: the warm-up creates them first
public final class WarmUp {

    public static final String FIELD = "warm";

    private WarmUp() {
    }

    /**
     * Whether the request is the warm stored request (its {@code ext.fast}, as {@link PodShape} read it).
     */
    public static boolean isWarm(PodShape.Result shaped) {
        return shaped.fast() != null && shaped.fast().path(FIELD).asBoolean(false);
    }

    /**
     * {@code request} with every imp's bidders cut to the own book and those the imp answers from a stored bid
     * response.
     */
    public static BidRequest restrict(BidRequest request) {
        return request.toBuilder().imp(request.getImp().stream().map(WarmUp::restrict).toList()).build();
    }

    static Imp restrict(Imp imp) {
        if (imp.getExt() == null || !(imp.getExt().get("prebid") instanceof ObjectNode prebid)
                || !(prebid.get("bidder") instanceof ObjectNode)) {
            return imp;
        }
        final Set<String> stored = new HashSet<>();
        final JsonNode responses = prebid.path("storedbidresponse");
        if (responses.isArray()) {
            responses.forEach(entry -> stored.add(entry.path("bidder").asText("")));
        }
        final ObjectNode ext = imp.getExt().deepCopy();
        final ObjectNode bidder = (ObjectNode) ext.get("prebid").get("bidder");
        bidder.properties().removeIf(entry -> !PodCache.OWN_BOOK.equals(entry.getKey())
                && !stored.contains(entry.getKey()));
        return imp.toBuilder().ext(ext).build();
    }
}
