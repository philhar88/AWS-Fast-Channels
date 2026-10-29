package org.prebid.server.hooks.modules.fast.ctv;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Video;
import com.iab.openrtb.response.Bid;
import com.iab.openrtb.response.BidResponse;
import com.iab.openrtb.response.SeatBid;
import org.prebid.server.hooks.modules.fast.ctv.core.Candidate;
import org.prebid.server.hooks.modules.fast.ctv.core.PodRenderer;
import org.prebid.server.json.ObjectMapperProvider;
import org.prebid.server.model.CaseInsensitiveMultiMap;
import org.prebid.server.model.HttpRequestContext;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import java.util.stream.IntStream;
import java.util.stream.Stream;

/**
 * Shared test data: the Pod Decision Service contract's examples ({@code contracts/fixtures/pds/<call>/*.json},
 * system property {@code fast.ctv.fixtures}), turned into what Prebid Server hands the module; pods of 30 s slots
 * like the stored requests; VAST bids; and answers in the contract's shape for the hook tests.
 */
public final class Fixtures {

    public static final ObjectMapper MAPPER = ObjectMapperProvider.mapper();
    public static final String TICKET = "Tk3n_AbCdEfGhIjKlMnOpQ";
    public static final String SID = "0d6a0bd2-5a7e-4c3f-9a1e-2b6f2f0f9c11";
    public static final String EVID = "820461826";
    public static final String DECISION = SID + "." + EVID;
    public static final String TRACKER = "https://cdn.example/ads/b.gif?d={sid}.{evid}&c={crid}&e={event}&enr={enr}";

    private Fixtures() {
    }

    /**
     * The contract's examples of one call ({@code policy} or {@code select}), by file name: each holds
     * {@code description}, {@code context}, {@code request} and {@code response}.
     */
    public static Map<String, JsonNode> contractCases(String call) {
        final Path dir = Path.of(System.getProperty("fast.ctv.fixtures", "../../contracts/fixtures/pds"), call);
        final Map<String, JsonNode> cases = new TreeMap<>();
        try (Stream<Path> files = Files.list(dir)) {
            for (Path file : files.filter(path -> path.toString().endsWith(".json")).toList()) {
                cases.put(file.getFileName().toString(), MAPPER.readTree(Files.readString(file)));
            }
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        if (cases.isEmpty()) {
            throw new IllegalStateException("no contract examples in " + dir.toAbsolutePath());
        }
        return cases;
    }

    /**
     * MediaTailor's request as it would carry a {@code /policy} example's request: header templates, constants, the
     * break in the query and the viewer's headers.
     */
    public static HttpRequestContext policyContext(JsonNode request) {
        final CaseInsensitiveMultiMap.Builder headers = CaseInsensitiveMultiMap.builder()
                .add("X-Fast-T", request.path("t").asText())
                .add("X-Fast-Sid", request.path("sid").asText())
                .add("X-Fast-Pc", request.path("pc").asText())
                .add("X-Fast-Fv", request.path("fv").asText())
                .add("User-Agent", request.path("ua").asText())
                .add("X-Forwarded-For", request.path("xff").asText());
        if (!request.path("cfva").asText().isEmpty()) {
            headers.add("CloudFront-Viewer-Address", request.path("cfva").asText());
        }
        if (!request.path("ctx").asText().isEmpty()) {
            headers.add("x-fast-ctx", request.path("ctx").asText());
        }
        if (request.path("probe").asBoolean()) {
            headers.add("X-Fast-Probe", "1");
        }
        return HttpRequestContext.builder()
                .headers(headers.build())
                .queryParams(CaseInsensitiveMultiMap.builder()
                        .add("evid", request.path("evid").asText())
                        .add("upid", request.path("upid").asText())
                        .add("dur", request.path("dur_ms").asText()).build())
                .build();
    }

    /**
     * MediaTailor's request for a {@code /select} example's break.
     */
    public static HttpRequestContext selectContext(JsonNode request) {
        final CaseInsensitiveMultiMap.Builder headers = CaseInsensitiveMultiMap.builder()
                .add("X-Fast-Sid", request.path("sid").asText())
                .add("X-Fast-Pc", request.path("pc").asText());
        if (request.path("probe").asBoolean()) {
            headers.add("X-Fast-Probe", "1");
        }
        return HttpRequestContext.builder()
                .headers(headers.build())
                .queryParams(CaseInsensitiveMultiMap.builder()
                        .add("evid", request.path("evid").asText())
                        .add("dur", request.path("dur_ms").asText()).build())
                .build();
    }

    /**
     * The imps of a {@code /select} example's slots.
     */
    public static List<Imp> slots(JsonNode request) {
        final List<Imp> imps = new ArrayList<>();
        for (JsonNode slot : request.path("slots")) {
            final List<Integer> rqddurs = new ArrayList<>();
            slot.path("rqddurs").forEach(dur -> rqddurs.add(dur.asInt()));
            imps.add(Imp.builder().id(slot.path("impid").asText())
                    .video(Video.builder().poddur(slot.path("poddur").asInt()).rqddurs(rqddurs).podid(1).build())
                    .build());
        }
        return imps;
    }

    /**
     * The bids of a {@code /select} example as the module's candidates, keeping the example's references: a VAST
     * inline ad of the bid's {@code vast_dur_ms}, the own book's house mark in {@code ext.fast.house}.
     */
    public static List<Candidate> candidates(JsonNode request) {
        final PodRenderer renderer = new PodRenderer();
        final List<Candidate> candidates = new ArrayList<>();
        for (JsonNode node : request.path("bids")) {
            final List<String> adomain = new ArrayList<>();
            node.path("adomain").forEach(domain -> adomain.add(domain.asText()));
            final List<String> cat = new ArrayList<>();
            node.path("cat").forEach(value -> cat.add(value.asText()));
            final Bid.BidBuilder bid = Bid.builder()
                    .id(node.path("ref").asText())
                    .impid(node.path("impid").asText())
                    .price(new BigDecimal(node.path("price").asText()))
                    .crid(node.path("crid").asText(null))
                    .adomain(adomain)
                    .cat(cat.isEmpty() ? null : cat)
                    .dealid(node.path("dealid").asText(null))
                    .dur(node.path("dur").isInt() ? node.path("dur").asInt() : null)
                    .adm(vast(node.path("crid").asText(), null, node.path("vast_dur_ms").asInt()));
            if (node.path("house").asBoolean()) {
                bid.ext(MAPPER.createObjectNode().set("fast", MAPPER.createObjectNode().put("house", true)));
            }
            candidates.add(renderer.parse(node.path("ref").asText(), node.path("seat").asText(), bid.build()));
        }
        return candidates;
    }

    /**
     * One pod of {@code slots} 30 s slots for the own book, as {@code fast-pod-<30 * slots>s} holds it.
     */
    public static BidRequest pod(int slots) {
        final List<Imp> imps = IntStream.rangeClosed(1, slots).mapToObj(n -> Imp.builder()
                .id("s" + n)
                .video(Video.builder().poddur(30 * slots).rqddurs(List.of(30)).podid(1).build())
                .ext(MAPPER.createObjectNode().set("prebid", MAPPER.createObjectNode()
                        .set("bidder", MAPPER.createObjectNode().set("ownbook", MAPPER.createObjectNode()))))
                .build()).toList();
        return BidRequest.builder().id("r").imp(imps).tmax(500L).build();
    }

    public static String vast(String crid, String impression) {
        return vast(crid, impression, 30_030);
    }

    public static String vast(String crid, String impression, int durationMs) {
        final String impressionXml = impression != null ? "<Impression><![CDATA[" + impression + "]]></Impression>" : "";
        final String duration = "%02d:%02d:%02d.%03d".formatted(durationMs / 3_600_000, durationMs / 60_000 % 60,
                durationMs / 1000 % 60, durationMs % 1000);
        return "<VAST version=\"4.2\"><Ad id=\"" + crid + "\"><InLine><AdSystem>test</AdSystem><AdTitle>" + crid
                + "</AdTitle>" + impressionXml + "<Creatives><Creative><Linear><Duration>" + duration + "</Duration>"
                + "<MediaFiles><MediaFile delivery=\"streaming\" type=\"application/x-mpegURL\" width=\"1920\""
                + " height=\"1080\"><![CDATA[https://cdn.example/ads/" + crid + "/main.m3u8]]></MediaFile>"
                + "</MediaFiles></Linear></Creative></Creatives></InLine></Ad></VAST>";
    }

    public static Bid bid(String id, String impid, String crid, String adomain, double price) {
        return Bid.builder().id(id).impid(impid).crid(crid).adomain(List.of(adomain)).price(BigDecimal.valueOf(price))
                .dur(30).adm(vast(crid, null)).build();
    }

    public static Bid house(String id, String impid, String crid) {
        return bid(id, impid, crid, crid + ".fast.example", 0.01).toBuilder()
                .ext(MAPPER.createObjectNode().set("fast", MAPPER.createObjectNode().put("house", true))).build();
    }

    public static SeatBid seat(String seat, Bid... bids) {
        return SeatBid.builder().seat(seat).bid(List.of(bids)).build();
    }

    public static BidResponse response(SeatBid... seats) {
        return BidResponse.builder().id("r").seatbid(List.of(seats)).build();
    }

    /**
     * A {@code /policy} answer in the contract's shape for {@link #DECISION}: {@code mode} with {@code bidders}
     * (bidder codes, empty parameters), the viewer's IP, a connected TV and first-party data for the own book.
     */
    public static ObjectNode policyAnswer(String mode, String... bidders) {
        final ObjectNode answer = MAPPER.createObjectNode().put("v", 1).put("decision_id", DECISION)
                .put("mode", mode).put("reason", "missing");
        final ObjectNode names = answer.putObject("bidders");
        for (String bidder : bidders) {
            names.putObject(bidder);
        }
        answer.putObject("regs").put("coppa", "KIDS".equals(mode) ? 1 : 0).putNull("gpp").putNull("gpp_sid");
        answer.putObject("device").put("ip", "81.2.69.160").putNull("ipv6").put("devicetype", 3);
        answer.putNull("app_content");
        answer.putNull("user");
        answer.putObject("fpd").putArray("bidders").add("ownbook");
        answer.putArray("deals");
        return answer;
    }

    /**
     * A {@code /select} answer in the contract's shape for the {@code b0..b3} bids of a two-slot auction
     * ({@code house1, house2, sim1, sim2}): {@code sim1} and {@code sim2} in sequence, {@code house1} and
     * {@code house2} as their standalones, and trackers for every ad but {@code house2}.
     */
    public static ObjectNode selectAnswer() {
        final ObjectNode answer = MAPPER.createObjectNode().put("v", 1);
        final ArrayNode pod = answer.putArray("pod");
        pod.addObject().put("impid", "s1").put("ref", "b2").put("sequence", 1);
        pod.addObject().put("impid", "s2").put("ref", "b3").put("sequence", 2);
        final ArrayNode standalone = answer.putArray("standalone");
        standalone.addObject().put("impid", "s1").put("ref", "b0");
        standalone.addObject().put("impid", "s2").put("ref", "b1");
        final ObjectNode trackers = answer.putObject("trackers");
        trackers.putArray("b2").add(pdsTracker("sim1"));
        trackers.putArray("b3").add(pdsTracker("sim2"));
        trackers.putArray("b0").add(pdsTracker("house1"));
        return answer.put("reason", "filled");
    }

    /**
     * The Pod Decision Service's tracker of one ad of {@link #DECISION} (content-enrichment arm 1).
     */
    public static String pdsTracker(String crid) {
        return "https://cdn.example/ads/b.gif?d=" + DECISION + "&c=" + crid + "&e=impression&enr=1";
    }

    /**
     * MediaTailor's request as Prebid Server sees it: the header templates, the query and the viewer's user agent.
     */
    public static HttpRequestContext mediaTailorRequest(String ticket, String... extraHeaders) {
        final CaseInsensitiveMultiMap.Builder headers = CaseInsensitiveMultiMap.builder()
                .add("Content-Type", "application/json")
                .add("X-Fast-Origin", "origin-secret-value")
                .add("X-Fast-T", ticket)
                .add("X-Fast-Sid", SID)
                .add("X-Fast-Pc", "fastm-demo-p-a")
                .add("X-Fast-Fv", "r0007")
                .add("User-Agent", "Mozilla/5.0 (SMART-TV; Linux; Tizen 7.0) AppleWebKit/537.36")
                .add("X-Forwarded-For", "198.51.100.7, 203.0.113.20, 192.0.2.30");
        for (int i = 0; i + 1 < extraHeaders.length; i += 2) {
            headers.add(extraHeaders[i], extraHeaders[i + 1]);
        }
        return HttpRequestContext.builder()
                .headers(headers.build())
                .queryParams(CaseInsensitiveMultiMap.builder().add("evid", EVID).add("upid", "tears-of-steel")
                        .add("dur", "60060").build())
                .build();
    }
}
