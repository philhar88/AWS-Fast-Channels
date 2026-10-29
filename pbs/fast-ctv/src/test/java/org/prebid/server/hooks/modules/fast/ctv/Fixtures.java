package org.prebid.server.hooks.modules.fast.ctv;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Video;
import com.iab.openrtb.response.Bid;
import com.iab.openrtb.response.BidResponse;
import com.iab.openrtb.response.SeatBid;
import org.prebid.server.json.ObjectMapperProvider;
import org.prebid.server.model.CaseInsensitiveMultiMap;
import org.prebid.server.model.HttpRequestContext;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.math.BigDecimal;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.stream.IntStream;

/**
 * Shared test data: the Pod Decision Service fixtures in {@code contracts/fixtures/pds} (system property
 * {@code fast.ctv.fixtures}), pods of 30 s slots like the stored requests, and VAST bids.
 */
public final class Fixtures {

    public static final ObjectMapper MAPPER = ObjectMapperProvider.mapper();
    public static final String TICKET = "Tk3n_AbCdEfGhIjKlMnOpQ";
    public static final String SID = "0d6a0bd2-5a7e-4c3f-9a1e-2b6f2f0f9c11";
    public static final String EVID = "820461826";
    public static final String TRACKER = "https://cdn.example/ads/b.gif?d={sid}.{evid}&c={crid}&e={event}&enr={enr}";

    private Fixtures() {
    }

    public static JsonNode pds(String name) {
        final Path dir = Path.of(System.getProperty("fast.ctv.fixtures", "../../contracts/fixtures/pds"));
        try {
            return MAPPER.readTree(Files.readString(dir.resolve(name)));
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    public static ObjectNode pdsObject(String name) {
        return (ObjectNode) pds(name);
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
        final String impressionXml = impression != null ? "<Impression><![CDATA[" + impression + "]]></Impression>" : "";
        return "<VAST version=\"4.2\"><Ad id=\"" + crid + "\"><InLine><AdSystem>test</AdSystem><AdTitle>" + crid
                + "</AdTitle>" + impressionXml + "<Creatives><Creative><Linear><Duration>00:00:30.030</Duration>"
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
