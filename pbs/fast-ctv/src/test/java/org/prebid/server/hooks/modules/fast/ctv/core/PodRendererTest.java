package org.prebid.server.hooks.modules.fast.ctv.core;

import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.response.Bid;
import com.iab.openrtb.response.BidResponse;
import org.junit.jupiter.api.Test;
import org.prebid.server.hooks.modules.fast.ctv.core.Selection.Pick;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.bid;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.house;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.response;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.seat;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.vast;

public class PodRendererTest {

    private static final String OUR = "https://cdn.example/ads/b.gif?c=";

    private final PodRenderer target = new PodRenderer();

    @Test
    public void candidatesAreTheBidsForTheRequestsSlotsAndOnlyVastAdsAreUsable() {
        final BidResponse response = response(
                seat("amt", bid("a1", "s1", "sim1", "brand-a.example", 12.5),
                        bid("a2", "s1", "banner", "brand-x.example", 3.0).toBuilder().adm("<a href='x'>ad</a>").build(),
                        bid("a3", "s9", "elsewhere", "brand-y.example", 9.0),
                        bid("a4", "s2", "empty", "brand-z.example", 9.0).toBuilder().adm("<VAST version='4.2'/>").build()),
                seat("ownbook", house("h1", "s2", "house1")));

        final List<Candidate> candidates = target.candidates(response, pod(2).getImp());

        assertThat(candidates).extracting(Candidate::ref, Candidate::crid, Candidate::house, Candidate::usable)
                .containsExactly(org.assertj.core.groups.Tuple.tuple("b0", "sim1", false, true),
                        org.assertj.core.groups.Tuple.tuple("b1", "banner", false, false),
                        org.assertj.core.groups.Tuple.tuple("b2", "empty", false, false),
                        org.assertj.core.groups.Tuple.tuple("b3", "house1", true, true));
        assertThat(candidates.get(1).kind()).isNull();
        assertThat(candidates.getFirst().vastDurMs()).isEqualTo(30030);
        assertThat(candidates.getFirst().kind()).isEqualTo(Candidate.Kind.INLINE);
        assertThat(candidates.getFirst().hasImpression()).isFalse();
    }

    @Test
    public void aDocumentTypeDeclarationIsRefused() {
        final String xxe = "<?xml version=\"1.0\"?><!DOCTYPE VAST [<!ENTITY x SYSTEM \"file:///etc/passwd\">]>"
                + "<VAST version=\"4.2\"><Ad id=\"x\"><InLine><AdTitle>&x;</AdTitle></InLine></Ad></VAST>";

        assertThat(target.parse("b0", "amt", bid("a1", "s1", "x", "x.example", 1).toBuilder().adm(xxe).build())
                .usable()).isFalse();
    }

    @Test
    public void wrappersAreCandidatesWithoutADuration() {
        final String wrapper = "<VAST version=\"4.2\"><Ad id=\"w\"><Wrapper><AdSystem>s</AdSystem>"
                + "<Impression><![CDATA[https://t.example/i]]></Impression>"
                + "<VASTAdTagURI><![CDATA[https://ads.example/vast]]></VASTAdTagURI></Wrapper></Ad></VAST>";

        final Candidate candidate = target.parse("b0", "x", bid("w1", "s1", "w", "w.example", 2).toBuilder()
                .adm(wrapper).build());

        assertThat(candidate.kind()).isEqualTo(Candidate.Kind.WRAPPER);
        assertThat(candidate.vastDurMs()).isNull();
        assertThat(candidate.hasImpression()).isTrue();
    }

    @Test
    public void renderShouldSequenceWinnersAsSelectedThenAddStandalonesOnlyWithBuffet() {
        final Map<String, Candidate> byRef = byRef(response(
                seat("amt", bid("a1", "s1", "sim1", "brand-a.example", 12.5), bid("a2", "s2", "sim2", "b.example", 9)),
                seat("ownbook", house("h1", "s1", "house1"))), pod(2));
        final Selection selection = new Selection(
                List.of(new Pick("s2", "b1", 2), new Pick("s1", "b0", 1)), List.of(new Pick("s1", "b2", null)),
                Map.of(), "OK");

        final String withBuffet = target.render(selection, byRef, true, c -> List.of(OUR + c.crid()));
        final String withoutBuffet = target.render(selection, byRef, false, c -> List.of(OUR + c.crid()));

        assertThat(ads(withBuffet)).containsExactly("sim1#1", "sim2#2", "house1#");
        assertThat(ads(withoutBuffet)).containsExactly("sim1#1", "sim2#2");
        assertThat(withBuffet).startsWith("<VAST version=\"4.2\">").endsWith("</VAST>");
    }

    @Test
    public void everyAdGetsItsNoticesTheSelectionsTrackersAndOurTracker() {
        final Bid winner = bid("a1", "s1", "sim1", "brand-a.example", 12.5).toBuilder()
                .burl("https://t.example/b?p=${AUCTION_PRICE}").nurl("https://t.example/n").build();
        final Map<String, Candidate> byRef = byRef(response(seat("amt", winner)), pod(1));
        final Selection selection = new Selection(List.of(new Pick("s1", "b0", 1)), List.of(),
                Map.of("b0", List.of("https://verify.example/i")), "OK");

        final String vast = target.render(selection, byRef, true, c -> {
            final List<String> urls = new java.util.ArrayList<>(PodRenderer.noticeUrls(c.bid()));
            urls.addAll(selection.trackers().get(c.ref()));
            urls.add(OUR + c.crid());
            urls.add(OUR + c.crid());
            return urls;
        });

        assertThat(impressions(vast)).containsExactly("https://t.example/b?p=12.5", "https://t.example/n",
                "https://verify.example/i", OUR + "sim1");
    }

    @Test
    public void aThirdPartyAdWithNoImpressionGetsOurTracker() {
        final Bid thirdParty = bid("x1", "s1", "dsp1", "dsp.example", 4).toBuilder().adm(vast("dsp1", null)).build();
        final Map<String, Candidate> byRef = byRef(response(seat("dsp", thirdParty)), pod(1));
        assertThat(byRef.get("b0").hasImpression()).isFalse();

        final String vast = target.render(new Selection(List.of(new Pick("s1", "b0", 1)), List.of(), Map.of(), "OK"),
                byRef, true, c -> List.of(OUR + c.crid()));

        assertThat(impressions(vast)).containsExactly(OUR + "dsp1");
        assertThat(vast.indexOf("<Impression>")).isGreaterThan(vast.indexOf("</AdTitle>"))
                .isLessThan(vast.indexOf("<Creatives>"));
    }

    @Test
    public void existingImpressionsAreKeptAndOursFollows() {
        final Bid bid = bid("a1", "s1", "sim1", "a.example", 4).toBuilder()
                .adm(vast("sim1", "https://theirs.example/i")).build();
        final Map<String, Candidate> byRef = byRef(response(seat("amt", bid)), pod(1));

        final String vast = target.render(new Selection(List.of(new Pick("s1", "b0", 1)), List.of(), Map.of(), "OK"),
                byRef, true, c -> List.of(OUR + c.crid()));

        assertThat(impressions(vast)).containsExactly("https://theirs.example/i", OUR + "sim1");
    }

    @Test
    public void anEmptyPodIsAnEmptyVast() {
        assertThat(target.render(Selection.empty("EMPTY_POD"), Map.of(), true, c -> List.of()))
                .isEqualTo(PodRenderer.EMPTY_VAST);
    }

    @Test
    public void aSelectionNamingAnUnknownBidIsRefused() {
        assertThatThrownBy(() -> target.render(new Selection(List.of(new Pick("s1", "b7", 1)), List.of(), Map.of(),
                "OK"), Map.of(), true, c -> List.of())).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    public void houseFallbackFillsSlotsWithHouseAdsWithinPoddur() {
        final BidRequest request = pod(4).toBuilder().imp(pod(4).getImp().stream()
                .map(imp -> imp.toBuilder().video(imp.getVideo().toBuilder().poddur(90).build()).build()).toList())
                .build();
        final BidResponse response = response(
                seat("amt", bid("a1", "s1", "sim1", "brand-a.example", 35)),
                seat("ownbook", house("h1", "s1", "house1"), house("h2", "s2", "house2"), house("h3", "s3", "house3"),
                        house("h4", "s4", "house4"), house("h5", "s1", "house5")));
        final List<Candidate> candidates = target.candidates(response, request.getImp());

        final Selection selection = target.houseFallback(request.getImp(), candidates, true, "SELECT_HTTP_503");

        assertThat(selection.reason()).isEqualTo("SELECT_FALLBACK SELECT_HTTP_503");
        assertThat(selection.pod()).extracting(Pick::impid, Pick::sequence).containsExactly(
                org.assertj.core.groups.Tuple.tuple("s1", 1), org.assertj.core.groups.Tuple.tuple("s2", 2),
                org.assertj.core.groups.Tuple.tuple("s3", 3));
        assertThat(selection.standalone()).extracting(Pick::impid).containsExactly("s1");
        assertThat(target.houseFallback(request.getImp(), candidates, false, "x").standalone()).isEmpty();
    }

    @Test
    public void houseFallbackNeverRepeatsACreativeOrAnAdvertiser() {
        final BidResponse response = response(seat("ownbook", house("h1", "s1", "house1"),
                house("h2", "s2", "house1"), house("h3", "s2", "house2").toBuilder().adomain(List.of("house1.fast.example"))
                        .build(), house("h4", "s2", "house3")));
        final List<Candidate> candidates = target.candidates(response, pod(2).getImp());

        final Selection selection = target.houseFallback(pod(2).getImp(), candidates, false, "x");

        assertThat(selection.pod()).extracting(pick -> candidatesByRef(candidates).get(pick.ref()).crid())
                .containsExactly("house1", "house3");
    }

    private Map<String, Candidate> byRef(BidResponse response, BidRequest request) {
        return candidatesByRef(target.candidates(response, request.getImp()));
    }

    private static Map<String, Candidate> candidatesByRef(List<Candidate> candidates) {
        final Map<String, Candidate> byRef = new LinkedHashMap<>();
        candidates.forEach(candidate -> byRef.put(candidate.ref(), candidate));
        return byRef;
    }

    /**
     * {@code <crid>#<sequence>} of every rendered ad, in document order.
     */
    private static List<String> ads(String vast) {
        final Matcher matcher = Pattern.compile("<Ad id=\"[^\"]*\"( sequence=\"(\\d+)\")?>.*?<AdTitle>([^<]+)</AdTitle>")
                .matcher(vast);
        final List<String> ads = new java.util.ArrayList<>();
        while (matcher.find()) {
            ads.add(matcher.group(3) + "#" + (matcher.group(2) != null ? matcher.group(2) : ""));
        }
        return ads;
    }

    private static List<String> impressions(String vast) {
        final Matcher matcher = Pattern.compile("<Impression><!\\[CDATA\\[(.*?)]]></Impression>").matcher(vast);
        final List<String> urls = new java.util.ArrayList<>();
        while (matcher.find()) {
            urls.add(matcher.group(1));
        }
        return urls;
    }
}
