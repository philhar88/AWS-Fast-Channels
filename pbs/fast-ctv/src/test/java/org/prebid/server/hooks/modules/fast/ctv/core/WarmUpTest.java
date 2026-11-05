package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Imp;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;

public class WarmUpTest {

    @Test
    public void aWarmRequestKeepsOnlyTheOwnBookAndTheBiddersItAnswersFromStoredResponses() {
        final BidRequest request = withBidders(pod(2), List.of("amt", "echo26", "appnexus"), List.of("amt"));

        assertThat(WarmUp.restrict(request).getImp()).extracting(WarmUpTest::bidderNames)
                .containsOnly(List.of("ownbook", "amt"));
    }

    @Test
    public void withoutAStoredResponseNoPartnerIsKept() {
        final BidRequest request = withBidders(pod(1), List.of("amt"), List.of());

        assertThat(WarmUp.restrict(request).getImp()).extracting(WarmUpTest::bidderNames)
                .containsOnly(List.of("ownbook"));
    }

    @Test
    public void onlyTheWarmMarkMakesAWarmRequest() {
        final ObjectNode warm = MAPPER.createObjectNode().put("slot_s", 30).put("pod_s", "30").put("warm", true);
        final ObjectNode plain = MAPPER.createObjectNode().put("slot_s", 30).put("pod_s", "30");

        assertThat(WarmUp.isWarm(new PodShape.Result(pod(1), 30, 1, warm, null, null))).isTrue();
        assertThat(WarmUp.isWarm(new PodShape.Result(pod(1), 30, 1, plain, null, null))).isFalse();
        assertThat(WarmUp.isWarm(new PodShape.Result(pod(1), null, 0, null, null, null))).isFalse();
    }

    /** {@code request} whose imps name {@code bidders} beside the own book and answer {@code stored} from stored
     * bid responses. */
    static BidRequest withBidders(BidRequest request, List<String> bidders, List<String> stored) {
        return request.toBuilder().imp(request.getImp().stream().map(imp -> {
            final ObjectNode ext = imp.getExt().deepCopy();
            final ObjectNode prebid = (ObjectNode) ext.get("prebid");
            bidders.forEach(name -> ((ObjectNode) prebid.get("bidder")).putObject(name));
            if (!stored.isEmpty()) {
                final ArrayNode responses = prebid.putArray("storedbidresponse");
                stored.forEach(name -> responses.addObject().put("bidder", name).put("id", "warm-" + name));
            }
            return imp.toBuilder().ext(ext).build();
        }).toList()).build();
    }

    static List<String> bidderNames(Imp imp) {
        final List<String> names = new ArrayList<>();
        imp.getExt().path("prebid").path("bidder").properties().forEach(entry -> names.add(entry.getKey()));
        return names;
    }
}
