package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.junit.jupiter.api.Test;
import org.prebid.server.hooks.modules.fast.ctv.Fixtures;
import org.prebid.server.hooks.modules.fast.ctv.core.Selection.Pick;
import org.prebid.server.vertx.httpclient.HttpClient;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.Mockito.mock;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.bid;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.house;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pds;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.response;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.seat;

public class SelectClientTest {

    private final SelectClient target = new SelectClient(mock(HttpClient.class), MAPPER,
            "http://pds.fastm.fast.internal/select", 100);
    private final List<Candidate> candidates = new PodRenderer().candidates(response(
            seat("ownbook", house("h1", "s1", "house1"), house("h2", "s2", "house2")),
            seat("amt", bid("a1", "s1", "sim1", "brand-a.example", 12.5),
                    bid("a2", "s2", "sim2", "brand-b.example", 22.5))), pod(2).getImp());

    @Test
    public void theRequestIsTheContractsSelectRequest() throws Exception {
        final Signals signals = Signals.of(Fixtures.mediaTailorRequest(""));
        final PolicyDecision policy = new PolicyDecision(
                PolicyPatch.parse(pds("policy-response-contextual.json")), null, 4);

        final ObjectNode request = target.request(signals, policy, pod(2).getImp(), candidates);

        assertThat(MAPPER.readTree(request.toString())).isEqualTo(pds("select-request.json"));
    }

    @Test
    public void withoutAPolicyTheModeIsNullAndTheFallbackIsNamed() {
        final ObjectNode request = target.request(Signals.of(Fixtures.mediaTailorRequest("")),
                new PolicyDecision(null, "POLICY_HTTP_500", 2), pod(2).getImp(), candidates);

        assertThat(request.path("mode").isNull()).isTrue();
        assertThat(request.path("policy_fallback").asText()).isEqualTo("POLICY_HTTP_500");
        assertThat(request.path("decision_id").asText()).isEqualTo(Fixtures.SID + "." + Fixtures.EVID);
    }

    @Test
    public void theResponseFixtureIsAPodWithStandalonesAndTrackers() {
        final Selection selection = SelectClient.parse(pds("select-response.json"), byRef());

        assertThat(selection.pod()).containsExactly(new Pick("s1", "b2", 1), new Pick("s2", "b3", 2));
        assertThat(selection.standalone()).containsExactly(new Pick("s1", "b0", null), new Pick("s2", "b1", null));
        assertThat(selection.trackers()).containsEntry("b2", List.of("https://verify.example/imp?c=sim1"));
        assertThat(selection.reason()).isEqualTo("OK");
    }

    @Test
    public void malformedResponsesAreRefused() throws Exception {
        final List<String> bodies = List.of(
                "{\"v\":2,\"pod\":[]}",
                "{\"v\":1,\"pod\":[{\"ref\":\"b9\",\"sequence\":1}]}",
                "{\"v\":1,\"pod\":[{\"ref\":\"b0\",\"sequence\":1},{\"ref\":\"b1\",\"sequence\":1}]}",
                "{\"v\":1,\"pod\":[{\"ref\":\"b0\",\"sequence\":0}]}",
                "{\"v\":1,\"pod\":[{\"ref\":\"b0\",\"sequence\":1}],\"standalone\":[{\"ref\":\"b0\"}]}",
                "{\"v\":1,\"pod\":{}}",
                "{\"v\":1,\"pod\":[],\"trackers\":{\"b0\":[\"javascript:alert(1)\"]}}",
                "{\"v\":1,\"pod\":[],\"trackers\":{\"b9\":[]}}");
        for (String body : bodies) {
            final JsonNode answer = MAPPER.readTree(body);

            assertThatThrownBy(() -> SelectClient.parse(answer, byRef())).as(body)
                    .isInstanceOf(IllegalArgumentException.class);
        }
    }

    private Map<String, Candidate> byRef() {
        final Map<String, Candidate> byRef = new LinkedHashMap<>();
        candidates.forEach(candidate -> byRef.put(candidate.ref(), candidate));
        return byRef;
    }
}
