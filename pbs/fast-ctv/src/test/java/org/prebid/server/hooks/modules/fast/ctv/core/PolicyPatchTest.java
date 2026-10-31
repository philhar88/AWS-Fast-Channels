package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.App;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Content;
import com.iab.openrtb.request.Device;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Regs;
import org.junit.jupiter.api.Test;
import org.prebid.server.vertx.httpclient.HttpClient;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.DECISION;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.contractCases;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.policyAnswer;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.policyContext;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;

public class PolicyPatchTest {

    private final PolicyClient client = new PolicyClient(mock(HttpClient.class), MAPPER,
            "http://pds.fastm.fast.internal/policy", 50);

    @Test
    public void everyContractExampleIsAskedAndAppliedAsTheContractSays() throws Exception {
        for (Map.Entry<String, JsonNode> example : contractCases("policy").entrySet()) {
            final JsonNode request = example.getValue().path("request");
            final JsonNode response = example.getValue().path("response");
            final Signals signals = Signals.of(policyContext(request));

            assertThat(signals.problem()).as(example.getKey()).isNull();
            assertThat(MAPPER.readTree(client.question(signals).toString())).as(example.getKey())
                    .isEqualTo(MAPPER.readTree(request.toString()));

            final PolicyPatch patch = PolicyPatch.parse(response, signals.decisionId());
            final BidRequest stored = pod(2).toBuilder().device(Device.builder().ip("198.51.100.99").build()).build();
            final BidRequest patched = patch.apply(stored, MAPPER);

            assertThat(patch.mode().name()).as(example.getKey()).isEqualTo(response.path("mode").asText());
            final List<String> expected = new ArrayList<>(List.of("ownbook"));
            if (patch.mode().allowsThirdParties()) {
                response.path("bidders").fieldNames().forEachRemaining(expected::add);
            }
            assertThat(patched.getImp()).as(example.getKey()).extracting(PolicyPatchTest::bidderNames)
                    .containsOnly(expected);
            assertThat(patched.getRegs().getCoppa()).as(example.getKey())
                    .isEqualTo(response.path("regs").path("coppa").asInt());
            final JsonNode ip = response.path("device").path("ip");
            assertThat(patched.getDevice().getIp()).as(example.getKey()).isEqualTo(ip.isNull() ? null : ip.asText());
            assertThat(patched.getDevice().getDevicetype()).as(example.getKey())
                    .isEqualTo(response.path("device").path("devicetype").asInt());
        }
    }

    @Test
    public void applyShouldSetRegsDeviceAndContentFieldsOverTheStoredRequest() {
        final BidRequest stored = pod(2).toBuilder()
                .regs(Regs.builder().coppa(0).gdpr(0).build())
                .app(App.builder().id("fastm-app")
                        .content(Content.builder().livestream(1).context(1).cattax(9).build()).build())
                .build();
        final ObjectNode answer = policyAnswer("CONTEXTUAL", "amt");
        answer.putObject("app_content").put("title", "Tears of Steel").put("cattax", 9)
                .putObject("ext").put("gtax", 9);
        ((ObjectNode) answer.get("app_content")).putArray("cat").add("652").add("325");
        object(answer, "regs").put("gpp", "DBABMA~CPXxRfAPXxRfAAfKABENB-CgAAAAAAAAAAYgAAAAAAAA").putArray("gpp_sid").add(7);

        final BidRequest patched = PolicyPatch.parse(answer, DECISION).apply(stored, MAPPER);

        assertThat(patched.getRegs().getGdpr()).isZero();
        assertThat(patched.getRegs().getCoppa()).isZero();
        assertThat(patched.getRegs().getGppSid()).containsExactly(7);
        assertThat(patched.getDevice().getIp()).isEqualTo("81.2.69.160");
        assertThat(patched.getDevice().getDevicetype()).isEqualTo(3);
        assertThat(patched.getApp().getId()).isEqualTo("fastm-app");
        assertThat(patched.getApp().getContent().getLivestream()).isEqualTo(1);
        assertThat(patched.getApp().getContent().getTitle()).isEqualTo("Tears of Steel");
        assertThat(patched.getApp().getContent().getCat()).containsExactly("652", "325");
        assertThat(patched.getApp().getContent().getExt().path("gtax").asInt()).isEqualTo(9);
        assertThat(patched.getExt().getPrebid().getData().getBidders()).containsExactly("ownbook");
    }

    @Test
    public void aNullViewerAddressRemovesTheOneTakenFromTheHeaders() {
        final ObjectNode answer = policyAnswer("CONTEXTUAL", "amt");
        object(answer, "device").putNull("ip");
        final BidRequest stored = pod(1).toBuilder()
                .device(Device.builder().ip("203.0.113.66").ipv6("2001:db8::66").ua("ua").build()).build();

        final Device device = PolicyPatch.parse(answer, DECISION).apply(stored, MAPPER).getDevice();

        assertThat(device.getIp()).isNull();
        assertThat(device.getIpv6()).isNull();
        assertThat(device.getUa()).isEqualTo("ua");
    }

    @Test
    public void safeAndKidsBreaksNeverGainABidder() {
        for (String mode : List.of("SAFE", "KIDS")) {
            final PolicyPatch patch = PolicyPatch.parse(policyAnswer(mode, "amt", "echo26"), DECISION);

            assertThat(patch.addedBidders()).as(mode).isEmpty();
            assertThat(patch.apply(pod(2), MAPPER).getImp()).as(mode).extracting(PolicyPatchTest::bidderNames)
                    .containsOnly(List.of("ownbook"));
        }
    }

    @Test
    public void userDataReachesTheRequestOnlyInFullMode() {
        final ObjectNode answer = policyAnswer("CONTEXTUAL", "amt");
        answer.putObject("user").putArray("eids").addObject().put("source", "media.example.com");

        assertThat(PolicyPatch.parse(answer, DECISION).apply(pod(1), MAPPER).getUser()).isNull();
        assertThat(PolicyPatch.parse(answer.put("mode", "FULL"), DECISION).apply(pod(1), MAPPER).getUser().getEids())
                .hasSize(1);
    }

    @Test
    public void dealsJoinEveryImpsPrivateMarketplace() {
        final ObjectNode answer = policyAnswer("FULL", "amt");
        ((ArrayNode) answer.get("deals")).addObject().put("id", "fast-direct-1").put("bidfloor", 8.0).putNull("at")
                .putArray("wseat");

        final BidRequest patched = PolicyPatch.parse(answer, DECISION).apply(pod(2), MAPPER);

        assertThat(patched.getImp()).allSatisfy(imp -> {
            assertThat(imp.getPmp().getDeals()).extracting("id").containsExactly("fast-direct-1");
            assertThat(imp.getPmp().getDeals().getFirst().getAt()).isNull();
        });
    }

    @Test
    public void parseShouldRefuseAnythingTheSchemaDoesNotDescribe() throws Exception {
        final List<ObjectNode> answers = new ArrayList<>(List.of(
                policyAnswer("CONTEXTUAL").put("v", 2),
                policyAnswer("ANYTHING"),
                policyAnswer("CONTEXTUAL").put("reason", "because"),
                policyAnswer("CONTEXTUAL").put("decision_id", "another-session.820461826"),
                policyAnswer("CONTEXTUAL").put("enr", 1),
                policyAnswer("CONTEXTUAL").put("bidders", "amt"),
                policyAnswer("CONTEXTUAL").put("regs", "coppa")));
        answers.add(policyAnswer("CONTEXTUAL"));
        answers.getLast().remove("decision_id");
        answers.add(policyAnswer("CONTEXTUAL"));
        object(answers.getLast(), "bidders").put("amt", true);
        answers.add(policyAnswer("CONTEXTUAL"));
        object(answers.getLast(), "regs").put("coppa", 2);
        answers.add(policyAnswer("CONTEXTUAL"));
        object(answers.getLast(), "regs").put("us_privacy", "1YNN");
        answers.add(policyAnswer("CONTEXTUAL"));
        object(answers.getLast(), "device").put("ua", "spoofed");
        answers.add(policyAnswer("CONTEXTUAL"));
        object(answers.getLast(), "device").put("devicetype", 9);
        answers.add(policyAnswer("CONTEXTUAL"));
        object(answers.getLast(), "fpd").put("bidders", "ownbook");
        answers.add(policyAnswer("CONTEXTUAL"));
        ((ArrayNode) answers.getLast().get("deals")).addObject().put("id", "d").put("bidfloor", -1);
        answers.add(policyAnswer("CONTEXTUAL"));
        ((ArrayNode) answers.getLast().get("deals")).addObject().put("id", "d").put("wseat", "amt");
        answers.add(policyAnswer("CONTEXTUAL"));
        answers.getLast().set("deals", MAPPER.createObjectNode());
        for (ObjectNode answer : answers) {
            assertThatThrownBy(() -> PolicyPatch.parse(answer, DECISION)).as(answer.toString())
                    .isInstanceOf(IllegalArgumentException.class);
        }
        for (String body : List.of("[]", "{}", "{\"v\":\"1\"}")) {
            final JsonNode answer = MAPPER.readTree(body);

            assertThatThrownBy(() -> PolicyPatch.parse(answer, DECISION)).as(body)
                    .isInstanceOf(IllegalArgumentException.class);
        }
    }

    private static ObjectNode object(ObjectNode parent, String name) {
        return (ObjectNode) parent.get(name);
    }

    private static List<String> bidderNames(Imp imp) {
        final List<String> names = new ArrayList<>();
        imp.getExt().path("prebid").path("bidder").properties().forEach(entry -> names.add(entry.getKey()));
        return names;
    }
}
