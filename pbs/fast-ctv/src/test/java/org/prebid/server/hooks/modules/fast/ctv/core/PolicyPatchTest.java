package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.App;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Content;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Regs;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pdsObject;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;

public class PolicyPatchTest {

    @Test
    public void parseShouldReadTheContextualFixture() {
        final PolicyPatch patch = PolicyPatch.parse(pdsObject("policy-response-contextual.json"));

        assertThat(patch.mode()).isEqualTo(PolicyPatch.Mode.CONTEXTUAL);
        assertThat(patch.reason()).isEqualTo("missing");
        assertThat(patch.decisionId()).endsWith(".820461826");
        assertThat(patch.addedBidders()).containsExactly("amt");
        assertThat(patch.fpdBidders()).containsExactly("ownbook");
        assertThat(patch.user()).isNull();
        assertThat(patch.enr()).isZero();
        assertThat(patch.changesRequest()).isTrue();
    }

    @Test
    public void applyShouldAddBiddersToEveryImpAndNeverRemoveTheOwnBook() {
        final BidRequest patched = PolicyPatch.parse(pdsObject("policy-response-contextual.json")).apply(pod(3), MAPPER);

        assertThat(patched.getImp()).extracting(PolicyPatchTest::bidderNames).containsOnly(List.of("ownbook", "amt"));
    }

    @Test
    public void applyShouldSetRegsDeviceAndContentFieldsOverTheStoredRequest() {
        final BidRequest stored = pod(2).toBuilder()
                .regs(Regs.builder().coppa(0).gdpr(0).build())
                .app(App.builder().id("fastm-app")
                        .content(Content.builder().livestream(1).context(1).cattax(9).build()).build())
                .build();

        final BidRequest patched = PolicyPatch.parse(pdsObject("policy-response-contextual.json")).apply(stored, MAPPER);

        assertThat(patched.getRegs().getGdpr()).isZero();
        assertThat(patched.getRegs().getCoppa()).isZero();
        assertThat(patched.getRegs().getGppSid()).containsExactly(7);
        assertThat(patched.getDevice().getIp()).isEqualTo("198.51.100.7");
        assertThat(patched.getDevice().getDevicetype()).isEqualTo(3);
        assertThat(patched.getApp().getId()).isEqualTo("fastm-app");
        assertThat(patched.getApp().getContent().getLivestream()).isEqualTo(1);
        assertThat(patched.getApp().getContent().getTitle()).isEqualTo("Tears of Steel");
        assertThat(patched.getApp().getContent().getCat()).containsExactly("652", "325");
        assertThat(patched.getApp().getContent().getExt().path("gtax").asInt()).isEqualTo(9);
        assertThat(patched.getExt().getPrebid().getData().getBidders()).containsExactly("ownbook");
    }

    @Test
    public void safeAndKidsBreaksNeverGainABidder() {
        for (String mode : List.of("SAFE", "KIDS")) {
            final ObjectNode answer = pdsObject("policy-response-contextual.json").put("mode", mode);

            final PolicyPatch patch = PolicyPatch.parse(answer);

            assertThat(patch.addedBidders()).as(mode).isEmpty();
            assertThat(patch.apply(pod(2), MAPPER).getImp()).as(mode).extracting(PolicyPatchTest::bidderNames)
                    .containsOnly(List.of("ownbook"));
        }
    }

    @Test
    public void userDataReachesTheRequestOnlyInFullMode() {
        final ObjectNode answer = pdsObject("policy-response-contextual.json");
        answer.putObject("user").putArray("eids").addObject().put("source", "media.example.com");

        assertThat(PolicyPatch.parse(answer).apply(pod(1), MAPPER).getUser()).isNull();
        assertThat(PolicyPatch.parse(answer.put("mode", "FULL")).apply(pod(1), MAPPER).getUser().getEids())
                .hasSize(1);
    }

    @Test
    public void dealsJoinEveryImpsPrivateMarketplace() {
        final ObjectNode answer = pdsObject("policy-response-contextual.json");
        answer.putArray("deals").addObject().put("id", "fast-direct-1").put("bidfloor", 8.0);

        final BidRequest patched = PolicyPatch.parse(answer).apply(pod(2), MAPPER);

        assertThat(patched.getImp()).allSatisfy(imp ->
                assertThat(imp.getPmp().getDeals()).extracting("id").containsExactly("fast-direct-1"));
    }

    @Test
    public void theSafeFixtureAddsNoBidder() {
        final PolicyPatch patch = PolicyPatch.parse(pdsObject("policy-response-safe.json"));

        assertThat(patch.mode()).isEqualTo(PolicyPatch.Mode.SAFE);
        assertThat(patch.addedBidders()).isEmpty();
    }

    @Test
    public void parseShouldRefuseAnythingButSchemaVersionOne() throws Exception {
        final List<String> bodies = List.of(
                "{\"v\":2,\"mode\":\"CONTEXTUAL\"}",
                "{\"mode\":\"CONTEXTUAL\"}",
                "{\"v\":1,\"mode\":\"ANYTHING\"}",
                "{\"v\":1,\"mode\":\"FULL\",\"bidders\":[\"amt\"]}",
                "{\"v\":1,\"mode\":\"FULL\",\"bidders\":{\"amt\":true}}",
                "{\"v\":1,\"mode\":\"FULL\",\"regs\":\"coppa\"}",
                "{\"v\":1,\"mode\":\"FULL\",\"fpd\":{\"bidders\":\"ownbook\"}}",
                "{\"v\":1,\"mode\":\"FULL\",\"deals\":{}}",
                "{\"v\":1,\"mode\":\"FULL\",\"enr\":2}",
                "[]");
        for (String body : bodies) {
            final JsonNode answer = MAPPER.readTree(body);

            assertThatThrownBy(() -> PolicyPatch.parse(answer)).as(body).isInstanceOf(IllegalArgumentException.class);
        }
    }

    private static List<String> bidderNames(Imp imp) {
        final List<String> names = new ArrayList<>();
        imp.getExt().path("prebid").path("bidder").properties().forEach(entry -> names.add(entry.getKey()));
        return names;
    }
}
