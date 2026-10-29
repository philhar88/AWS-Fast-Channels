package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Video;
import com.iab.openrtb.response.Bid;
import com.iab.openrtb.response.BidResponse;
import com.iab.openrtb.response.SeatBid;
import org.prebid.server.hooks.modules.fast.ctv.core.Selection.Pick;
import org.w3c.dom.Document;
import org.w3c.dom.Element;
import org.w3c.dom.Node;
import org.xml.sax.InputSource;
import org.xml.sax.helpers.DefaultHandler;

import javax.xml.XMLConstants;
import javax.xml.parsers.DocumentBuilder;
import javax.xml.parsers.DocumentBuilderFactory;
import javax.xml.parsers.ParserConfigurationException;
import javax.xml.transform.OutputKeys;
import javax.xml.transform.Transformer;
import javax.xml.transform.TransformerConfigurationException;
import javax.xml.transform.TransformerException;
import javax.xml.transform.TransformerFactory;
import javax.xml.transform.dom.DOMSource;
import javax.xml.transform.stream.StreamResult;
import java.io.StringReader;
import java.io.StringWriter;
import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.function.Function;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Turns an auction's bids into VAST candidates and renders a {@link Selection} as one VAST 4.2 document for
 * MediaTailor ({@code FOLLOW_AD_SEQUENCE}: winners carry {@code sequence} 1..n, standalones none).
 * <p>
 * A bid is a candidate only when its {@code adm} is a VAST document whose first {@code <Ad>} holds an
 * {@code <InLine>} or a {@code <Wrapper>}; a document type declaration is refused (no XML external entities). Every
 * rendered ad gets {@code burl} and {@code nurl} as {@code <Impression>} elements, with {@code ${AUCTION_PRICE}}
 * replaced, then the selection's extra trackers, then our own tracker: MediaTailor drops an inline ad that has no
 * impression. No price or deal decision is made here: pods are selected by the Pod Decision Service, and
 * {@link #houseFallback} only fills slots with the own book's house ads when it cannot be asked.
 */
public class PodRenderer {

    public static final String EMPTY_VAST = "<VAST version=\"4.2\"/>";
    public static final String FALLBACK = "SELECT_FALLBACK";

    private static final int MAX_ADM_CHARS = 128 * 1024;
    private static final String PRICE_MACRO = "${AUCTION_PRICE}";
    private static final Pattern DURATION = Pattern.compile("\\s*(\\d+):(\\d{2}):(\\d{2}(?:\\.\\d{1,3})?)\\s*");
    private static final List<String> IMPRESSION_PREDECESSORS = List.of("AdSystem", "AdTitle", "Error", "Impression");
    private static final DefaultHandler SILENT = new DefaultHandler();
    private static final ThreadLocal<DocumentBuilder> PARSER = ThreadLocal.withInitial(PodRenderer::newParser);
    private static final ThreadLocal<Transformer> WRITER = ThreadLocal.withInitial(PodRenderer::newWriter);

    /**
     * The usable bids of {@code response} for {@code imps}, in seat and bid order, with references
     * {@code b0, b1, ...}.
     */
    public List<Candidate> candidates(BidResponse response, List<Imp> imps) {
        final Set<String> impIds = new HashSet<>();
        imps.forEach(imp -> impIds.add(imp.getId()));
        final List<Candidate> candidates = new ArrayList<>();
        for (SeatBid seatBid : response != null && response.getSeatbid() != null ? response.getSeatbid() : List.<SeatBid>of()) {
            for (Bid bid : seatBid.getBid() != null ? seatBid.getBid() : List.<Bid>of()) {
                if (impIds.contains(bid.getImpid())) {
                    final Candidate candidate = parse("b" + candidates.size(), seatBid.getSeat(), bid);
                    if (candidate != null) {
                        candidates.add(candidate);
                    }
                }
            }
        }
        return candidates;
    }

    /**
     * {@code bid} as a candidate, or null when its {@code adm} is not a usable VAST ad.
     */
    public Candidate parse(String ref, String seat, Bid bid) {
        final String adm = bid.getAdm();
        if (adm == null || adm.isBlank() || adm.length() > MAX_ADM_CHARS) {
            return null;
        }
        final Element ad;
        try {
            final DocumentBuilder parser = PARSER.get();
            parser.reset();
            parser.setErrorHandler(SILENT);
            final Document doc = parser.parse(new InputSource(new StringReader(withPrice(adm, bid.getPrice()))));
            final Element root = doc.getDocumentElement();
            ad = "VAST".equals(root.getLocalName()) ? firstChild(root, "Ad") : null;
        } catch (Exception e) {
            return null;
        }
        final Element inline = ad != null ? firstChild(ad, "InLine") : null;
        final Element body = inline != null ? inline : ad != null ? firstChild(ad, "Wrapper") : null;
        if (body == null) {
            return null;
        }
        return new Candidate(ref, seat, bid, ad, inline != null ? Candidate.Kind.INLINE : Candidate.Kind.WRAPPER,
                inline != null ? durationMs(inline) : null, hasImpression(body), isHouse(bid));
    }

    /**
     * The house-only pod used when {@code /select} cannot answer: per slot, in slot order, the first house ad of
     * that slot that still fits {@code poddur} and repeats no {@code crid} or advertiser domain; with buffet, one
     * more such house ad per winner as its standalone.
     */
    public Selection houseFallback(List<Imp> imps, List<Candidate> candidates, boolean buffet, String reason) {
        final Map<String, List<Candidate>> houseBySlot = new HashMap<>();
        candidates.stream().filter(Candidate::house)
                .forEach(c -> houseBySlot.computeIfAbsent(c.impid(), id -> new ArrayList<>()).add(c));
        final int limit = podLimit(imps);
        final Set<String> claimed = new HashSet<>();
        final List<Pick> pod = new ArrayList<>();
        final List<Pick> standalone = new ArrayList<>();
        int used = 0;
        for (Imp imp : imps) {
            for (Candidate candidate : houseBySlot.getOrDefault(imp.getId(), List.of())) {
                final int dur = seconds(candidate, imp);
                if (used + dur <= limit && claim(candidate, claimed)) {
                    pod.add(new Pick(imp.getId(), candidate.ref(), pod.size() + 1));
                    used += dur;
                    break;
                }
            }
        }
        if (buffet) {
            for (Pick winner : List.copyOf(pod)) {
                houseBySlot.getOrDefault(winner.impid(), List.of()).stream()
                        .filter(candidate -> claim(candidate, claimed)).findFirst()
                        .ifPresent(candidate -> standalone.add(new Pick(winner.impid(), candidate.ref(), null)));
            }
        }
        return new Selection(pod, standalone, Map.of(), FALLBACK + " " + reason);
    }

    /**
     * {@code selection} as VAST 4.2: winners in {@code sequence} order, then the standalones unless {@code buffet} is
     * off. {@code impressions} gives each ad's impression URLs in order; duplicates are dropped.
     *
     * @throws IllegalArgumentException when the selection names a bid that is not a candidate
     */
    public String render(Selection selection, Map<String, Candidate> byRef, boolean buffet,
                         Function<Candidate, List<String>> impressions) {
        final List<Pick> winners = selection.pod().stream()
                .sorted((a, b) -> Integer.compare(a.sequence(), b.sequence())).toList();
        if (winners.isEmpty()) {
            return EMPTY_VAST;
        }
        final StringBuilder vast = new StringBuilder("<VAST version=\"4.2\">");
        for (Pick pick : winners) {
            vast.append(adXml(candidate(byRef, pick), pick.sequence(), impressions));
        }
        for (Pick pick : buffet ? selection.standalone() : List.<Pick>of()) {
            vast.append(adXml(candidate(byRef, pick), null, impressions));
        }
        return vast.append("</VAST>").toString();
    }

    /**
     * {@code burl}, then {@code nurl}, with {@code ${AUCTION_PRICE}} replaced.
     */
    public static List<String> noticeUrls(Bid bid) {
        final List<String> urls = new ArrayList<>(2);
        for (String url : new String[]{bid.getBurl(), bid.getNurl()}) {
            if (url != null && !url.isBlank()) {
                urls.add(withPrice(url, bid.getPrice()));
            }
        }
        return urls;
    }

    private static Candidate candidate(Map<String, Candidate> byRef, Pick pick) {
        final Candidate candidate = byRef.get(pick.ref());
        if (candidate == null) {
            throw new IllegalArgumentException("unknown bid reference " + pick.ref());
        }
        return candidate;
    }

    private static String adXml(Candidate candidate, Integer sequence, Function<Candidate, List<String>> impressions) {
        final Element ad = (Element) candidate.ad().cloneNode(true);
        if (sequence != null) {
            ad.setAttribute("sequence", sequence.toString());
        } else {
            ad.removeAttribute("sequence");
        }
        if (!ad.hasAttribute("id") && candidate.bid().getId() != null) {
            ad.setAttribute("id", candidate.bid().getId());
        }
        final Element body = Objects.requireNonNullElseGet(firstChild(ad, "InLine"), () -> firstChild(ad, "Wrapper"));
        final Node anchor = impressionAnchor(body);
        for (String url : new LinkedHashSet<>(impressions.apply(candidate))) {
            final Element impression = ad.getOwnerDocument().createElementNS(body.getNamespaceURI(), "Impression");
            impression.appendChild(ad.getOwnerDocument().createCDATASection(url));
            body.insertBefore(impression, anchor);
        }
        final StringWriter out = new StringWriter();
        final Transformer writer = WRITER.get();
        writer.reset();
        writer.setOutputProperty(OutputKeys.OMIT_XML_DECLARATION, "yes");
        try {
            writer.transform(new DOMSource(ad), new StreamResult(out));
        } catch (TransformerException e) {
            throw new IllegalStateException("cannot write ad " + candidate.ref(), e);
        }
        return out.toString();
    }

    private static int podLimit(List<Imp> imps) {
        return imps.stream().map(Imp::getVideo).filter(Objects::nonNull).map(Video::getPoddur)
                .filter(poddur -> poddur != null && poddur > 0).findFirst()
                .orElseGet(() -> imps.stream().mapToInt(PodRenderer::slotMax).sum());
    }

    private static int slotMax(Imp imp) {
        final Video video = imp.getVideo();
        if (video == null) {
            return 0;
        }
        if (video.getRqddurs() != null && !video.getRqddurs().isEmpty()) {
            return video.getRqddurs().stream().filter(Objects::nonNull).mapToInt(Integer::intValue).max().orElse(0);
        }
        return video.getMaxduration() != null ? video.getMaxduration() : 0;
    }

    private static int seconds(Candidate candidate, Imp imp) {
        if (candidate.bid().getDur() != null && candidate.bid().getDur() > 0) {
            return candidate.bid().getDur();
        }
        return candidate.vastDurMs() != null ? (int) Math.round(candidate.vastDurMs() / 1000.0) : slotMax(imp);
    }

    private static boolean claim(Candidate candidate, Set<String> claimed) {
        final Collection<String> keys = new ArrayList<>();
        if (!candidate.crid().isEmpty()) {
            keys.add("crid:" + candidate.crid());
        }
        candidate.adomain().forEach(domain -> keys.add("adomain:" + domain));
        if (keys.stream().anyMatch(claimed::contains)) {
            return false;
        }
        claimed.addAll(keys);
        return true;
    }

    private static boolean isHouse(Bid bid) {
        final JsonNode ext = bid.getExt();
        return ext != null && ext.path("fast").path("house").asBoolean(false);
    }

    private static Integer durationMs(Element inline) {
        for (Element creatives : children(inline, "Creatives")) {
            for (Element creative : children(creatives, "Creative")) {
                final Element linear = firstChild(creative, "Linear");
                final Element duration = linear != null ? firstChild(linear, "Duration") : null;
                final Matcher matcher = duration != null ? DURATION.matcher(duration.getTextContent()) : null;
                if (matcher != null && matcher.matches()) {
                    final double seconds = Integer.parseInt(matcher.group(1)) * 3600
                            + Integer.parseInt(matcher.group(2)) * 60 + Double.parseDouble(matcher.group(3));
                    return (int) Math.round(seconds * 1000);
                }
            }
        }
        return null;
    }

    private static boolean hasImpression(Element body) {
        return children(body, "Impression").stream().anyMatch(element -> !element.getTextContent().isBlank());
    }

    /**
     * New impressions go after the existing ones, else after {@code AdSystem}, {@code AdTitle} and {@code Error},
     * keeping VAST's element order.
     */
    private static Node impressionAnchor(Element body) {
        Node anchor = body.getFirstChild();
        for (Node node = body.getFirstChild(); node != null; node = node.getNextSibling()) {
            if (node instanceof Element element && IMPRESSION_PREDECESSORS.contains(element.getLocalName())) {
                anchor = node.getNextSibling();
            }
        }
        return anchor;
    }

    private static Element firstChild(Element parent, String localName) {
        final List<Element> matches = children(parent, localName);
        return matches.isEmpty() ? null : matches.getFirst();
    }

    private static List<Element> children(Element parent, String localName) {
        final List<Element> matches = new ArrayList<>();
        for (Node node = parent.getFirstChild(); node != null; node = node.getNextSibling()) {
            if (node instanceof Element element && localName.equals(element.getLocalName())) {
                matches.add(element);
            }
        }
        return matches;
    }

    private static String withPrice(String text, BigDecimal price) {
        return price != null ? text.replace(PRICE_MACRO, price.toPlainString()) : text;
    }

    private static DocumentBuilder newParser() {
        try {
            final DocumentBuilderFactory factory = DocumentBuilderFactory.newInstance();
            factory.setNamespaceAware(true);
            factory.setFeature(XMLConstants.FEATURE_SECURE_PROCESSING, true);
            factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
            factory.setXIncludeAware(false);
            factory.setExpandEntityReferences(false);
            return factory.newDocumentBuilder();
        } catch (ParserConfigurationException e) {
            throw new IllegalStateException(e);
        }
    }

    private static Transformer newWriter() {
        try {
            final TransformerFactory factory = TransformerFactory.newInstance();
            factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "");
            factory.setAttribute(XMLConstants.ACCESS_EXTERNAL_STYLESHEET, "");
            return factory.newTransformer();
        } catch (TransformerConfigurationException e) {
            throw new IllegalStateException(e);
        }
    }
}
