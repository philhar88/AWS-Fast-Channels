package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Video;
import com.iab.openrtb.response.Bid;
import org.junit.jupiter.api.Test;
import org.prebid.server.hooks.modules.fast.ctv.Fixtures;
import org.prebid.server.hooks.modules.fast.ctv.core.Selection.Pick;
import org.prebid.server.vertx.httpclient.HttpClient;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.DECISION;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.bid;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.contractCases;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.house;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.response;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.seat;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.selectAnswer;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.selectContext;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.slots;

public class SelectClientTest {

    private final SelectClient target = new SelectClient(mock(HttpClient.class), MAPPER,
            "http://pds.fastm.fast.internal/select", 100);
    private final List<Candidate> candidates = new PodRenderer().candidates(response(
            seat("ownbook", house("h1", "s1", "house1"), house("h2", "s2", "house2")),
            seat("amt", bid("a1", "s1", "sim1", "brand-a.example", 12.5),
                    bid("a2", "s2", "sim2", "brand-b.example", 22.5))), pod(2).getImp());

    @Test
    public void everyContractExampleIsAskedReadAndRenderedAsTheContractSays() throws Exception {
        for (Map.Entry<String, JsonNode> example : contractCases("select").entrySet()) {
            final JsonNode request = example.getValue().path("request");
            final JsonNode response = example.getValue().path("response");
            final Signals signals = Signals.of(selectContext(request));
            final List<Imp> imps = slots(request);
            final List<Candidate> bids = Fixtures.candidates(request);
            final PolicyDecision policy = request.path("mode").isNull()
                    ? new PolicyDecision(null, "POLICY_UNAVAILABLE TimeoutException", 50)
                    : new PolicyDecision(new PolicyPatch(DECISION, PolicyPatch.Mode.valueOf(request.path("mode")
                    .asText()), "missing", null, null, null, null, null, List.of(), null), null, 4);

            assertThat(SelectClient.problem(signals, imps)).as(example.getKey()).isNull();
            assertThat(MAPPER.readTree(target.request(signals, policy, imps, bids).toString())).as(example.getKey())
                    .isEqualTo(MAPPER.readTree(request.toString()));

            final Map<String, Candidate> byRef = new LinkedHashMap<>();
            bids.forEach(candidate -> byRef.put(candidate.ref(), candidate));
            final Selection selection = SelectClient.parse(response, byRef);
            final String vast = new PodRenderer().render(selection, byRef, true,
                    candidate -> selection.trackers().getOrDefault(candidate.ref(), List.of()));

            final List<String> expected = new ArrayList<>();
            response.path("pod").forEach(ad -> expected.add(byRef.get(ad.path("ref").asText()).crid() + "#"
                    + ad.path("sequence").asInt()));
            response.path("standalone").forEach(ad -> expected.add(byRef.get(ad.path("ref").asText()).crid() + "#"));
            assertThat(ads(vast)).as(example.getKey()).containsExactlyElementsOf(expected);
            response.path("trackers").forEach(urls -> urls.forEach(url ->
                    assertThat(vast).as(example.getKey()).contains("<Impression><![CDATA[" + url.asText() + "]]>")));
            assertThat(selection.reason()).as(example.getKey()).isEqualTo(response.path("reason").asText());
        }
    }

    @Test
    public void withoutAPolicyTheModeIsNullAndTheDecisionIsTheSessionsBreak() {
        final ObjectNode request = target.request(Signals.of(Fixtures.mediaTailorRequest("", "X-Fast-Probe", "1")),
                new PolicyDecision(null, "POLICY_HTTP_500", 2), pod(2).getImp(), candidates);

        assertThat(request.path("mode").isNull()).isTrue();
        assertThat(request.has("policy_fallback")).isFalse();
        assertThat(request.path("probe").asBoolean()).isTrue();
        assertThat(request.path("decision_id").asText()).isEqualTo(DECISION);
        assertThat(request.path("bids")).extracting(bid -> bid.path("adm_kind").asText()).containsOnly("InLine");
    }

    @Test
    public void bidsTheSchemaCannotCarryAreLeftOutAndUnusableAdmIsMarked() {
        final List<Candidate> mixed = new PodRenderer().candidates(response(
                seat("ownbook", house("h1", "s1", "house1").toBuilder().adm("<html/>").build()),
                seat("bad seat", bid("x1", "s1", "x", "x.example", 1)),
                seat("amt", bid("a1", "s1", "sim1", "brand-a.example", 12.5).toBuilder().price(null).build(),
                        bid("a2", "s1", "sim2", "brand-b.example", 22.5).toBuilder().dur(-1).cattax(0).build())),
                pod(1).getImp());

        final ObjectNode request = target.request(Signals.of(Fixtures.mediaTailorRequest("")), null,
                pod(1).getImp(), mixed);

        assertThat(request.path("bids")).extracting(bid -> bid.path("ref").asText()).containsExactly("b0", "b3");
        assertThat(request.path("bids").get(0).path("adm_kind").isNull()).isTrue();
        assertThat(request.path("bids").get(0).path("house").asBoolean()).isTrue();
        assertThat(request.path("bids").get(1).path("dur").isNull()).isTrue();
        assertThat(request.path("bids").get(1).path("cattax").isNull()).isTrue();
    }

    @Test
    public void theBidderIsTheRequestsBidderCodeNotTheAdapterBehindAnAlias() {
        final ObjectNode ext = MAPPER.createObjectNode();
        ext.putObject("prebid").putObject("meta").put("adaptercode", "generic");
        final List<Candidate> ownBook = new PodRenderer().candidates(response(
                seat("ownbook", house("h1", "s1", "house1").toBuilder().ext(ext.deepCopy()
                        .<ObjectNode>set("fast", MAPPER.createObjectNode().put("house", true))).build())),
                pod(1).getImp());

        final ObjectNode request = target.request(Signals.of(Fixtures.mediaTailorRequest("")), null,
                pod(1).getImp(), ownBook);

        assertThat(request.path("bids").get(0).path("bidder").asText()).isEqualTo("ownbook");
        assertThat(request.path("bids").get(0).path("house").asBoolean()).isTrue();
    }

    @Test
    public void aBreakTheContractCannotCarryIsNotAsked() {
        final Imp noPoddur = Imp.builder().id("s1").video(Video.builder().rqddurs(List.of(30)).build()).build();

        assertThat(SelectClient.problem(Signals.of(Fixtures.mediaTailorRequest("")), pod(2).getImp())).isNull();
        assertThat(SelectClient.problem(Signals.of(Fixtures.mediaTailorRequest("")), List.of(noPoddur)))
                .isEqualTo("slots[].poddur");
        assertThat(SelectClient.problem(Signals.of(Fixtures.mediaTailorRequest("")), pod(17).getImp()))
                .isEqualTo("slots");
        assertThat(SelectClient.problem(Signals.of(null), pod(1).getImp())).isEqualTo("sid");
        assertThat(target.select(null, "sid", Map.of(), 100).result().failure()).isEqualTo("SELECT_SKIPPED sid");
    }

    @Test
    public void theAnswerIsAPodWithStandalonesAndTrackers() {
        final Selection selection = SelectClient.parse(selectAnswer(), byRef());

        assertThat(selection.pod()).containsExactly(new Pick("s1", "b2", 1), new Pick("s2", "b3", 2));
        assertThat(selection.standalone()).containsExactly(new Pick("s1", "b0", null), new Pick("s2", "b1", null));
        assertThat(selection.trackers()).containsEntry("b2", List.of(Fixtures.pdsTracker("sim1")))
                .doesNotContainKey("b1");
        assertThat(selection.reason()).isEqualTo("filled");
    }

    @Test
    public void malformedAnswersAreRefused() throws Exception {
        final List<String> bodies = List.of(
                "{\"v\":2,\"pod\":[],\"reason\":\"empty\"}",
                "{\"v\":1,\"pod\":[]}",
                "{\"v\":1,\"pod\":[],\"reason\":\"OK\"}",
                "{\"v\":1,\"pod\":[],\"reason\":\"empty\",\"debug\":true}",
                "{\"v\":1,\"pod\":[{\"impid\":\"s1\",\"ref\":\"b9\",\"sequence\":1}],\"reason\":\"filled\"}",
                "{\"v\":1,\"pod\":[{\"impid\":\"s2\",\"ref\":\"b0\",\"sequence\":1}],\"reason\":\"filled\"}",
                "{\"v\":1,\"pod\":[{\"ref\":\"b0\",\"sequence\":1}],\"reason\":\"filled\"}",
                "{\"v\":1,\"pod\":[{\"impid\":\"s1\",\"ref\":\"b0\",\"sequence\":1,\"price\":1}],\"reason\":\"filled\"}",
                "{\"v\":1,\"pod\":[{\"impid\":\"s1\",\"ref\":\"b0\",\"sequence\":1},"
                        + "{\"impid\":\"s2\",\"ref\":\"b1\",\"sequence\":1}],\"reason\":\"filled\"}",
                "{\"v\":1,\"pod\":[{\"impid\":\"s1\",\"ref\":\"b0\",\"sequence\":0}],\"reason\":\"filled\"}",
                "{\"v\":1,\"pod\":[{\"impid\":\"s1\",\"ref\":\"b0\",\"sequence\":1}],"
                        + "\"standalone\":[{\"impid\":\"s1\",\"ref\":\"b0\"}],\"reason\":\"filled\"}",
                "{\"v\":1,\"pod\":{},\"reason\":\"empty\"}",
                "{\"v\":1,\"pod\":[{\"impid\":\"s1\",\"ref\":\"b0\",\"sequence\":1}],"
                        + "\"trackers\":{\"b0\":[\"javascript:alert(1)\"]},\"reason\":\"filled\"}",
                "{\"v\":1,\"pod\":[],\"trackers\":{\"b2\":[]},\"reason\":\"empty\"}");
        for (String body : bodies) {
            final JsonNode answer = MAPPER.readTree(body);

            assertThatThrownBy(() -> SelectClient.parse(answer, byRef())).as(body)
                    .isInstanceOf(IllegalArgumentException.class);
        }
    }

    @Test
    public void anUnusableBidCannotBePlaced() {
        final Candidate unusable = Candidate.unusable("b0", "amt", Bid.builder().impid("s1").crid("x")
                .price(BigDecimal.ONE).build(), false);
        final ObjectNode answer = MAPPER.createObjectNode().put("v", 1).put("reason", "filled");
        answer.putArray("pod").addObject().put("impid", "s1").put("ref", "b0").put("sequence", 1);

        assertThatThrownBy(() -> SelectClient.parse(answer, Map.of("b0", unusable)))
                .isInstanceOf(IllegalArgumentException.class);
    }

    private Map<String, Candidate> byRef() {
        final Map<String, Candidate> byRef = new LinkedHashMap<>();
        candidates.forEach(candidate -> byRef.put(candidate.ref(), candidate));
        return byRef;
    }

    static List<String> ads(String vast) {
        final Matcher matcher = Pattern.compile("<Ad id=\"[^\"]*\"( sequence=\"(\\d+)\")?>.*?<AdTitle>([^<]+)</AdTitle>")
                .matcher(vast);
        final List<String> ads = new ArrayList<>();
        while (matcher.find()) {
            ads.add(matcher.group(3) + "#" + (matcher.group(2) != null ? matcher.group(2) : ""));
        }
        return ads;
    }
}
