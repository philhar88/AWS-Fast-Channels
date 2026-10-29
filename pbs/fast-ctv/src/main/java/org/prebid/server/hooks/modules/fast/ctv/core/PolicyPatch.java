package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.App;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Content;
import com.iab.openrtb.request.Device;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Pmp;
import com.iab.openrtb.request.Regs;
import com.iab.openrtb.request.User;
import org.prebid.server.proto.openrtb.ext.request.ExtRequest;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The Pod Decision Service's answer to {@code /policy}, schema version 1, and how it changes the bid request.
 * <p>
 * The module applies the patch verbatim and only adds: the returned bidders join every imp beside the stored
 * request's own book, which is never removed. {@code regs}, {@code device} and {@code app_content} fields are set
 * over the request's; {@code user} only in {@code FULL} mode; first-party data is scoped to {@code fpd.bidders};
 * {@code deals} join every imp's private marketplace. {@code SAFE} and {@code KIDS} breaks never gain a bidder, whatever
 * the answer says. A response that does not match the schema is refused as a whole ({@link #parse} throws), and the
 * request stays as stored.
 *
 * @param decisionId the break's decision id ({@code <sid>.<evid>})
 * @param mode       the privacy mode of the break
 * @param reason     why the service chose the mode ({@code missing}, {@code replay}, ...)
 * @param bidders    bidder code to imp-level bidder parameters; added to every imp
 * @param regs       fields set on {@code regs}, or null
 * @param device     fields set on {@code device} (the viewer's IP and device type), or null
 * @param appContent fields set on {@code app.content}, or null
 * @param user       fields set on {@code user} in {@code FULL} mode, or null
 * @param fpdBidders bidders that may receive first-party data ({@code ext.prebid.data.bidders}), or empty
 * @param deals      OpenRTB deals added to every imp's {@code pmp}, or empty
 * @param enr        1 when the break's request carries content enrichment, else 0 (the tracker's {@code enr})
 */
public record PolicyPatch(String decisionId, Mode mode, String reason, ObjectNode bidders, ObjectNode regs,
                          ObjectNode device, ObjectNode appContent, ObjectNode user, List<String> fpdBidders,
                          ArrayNode deals, int enr) {

    public static final int VERSION = 1;

    /**
     * Privacy modes (what third parties may receive), as the Pod Decision Service decides them per break.
     */
    public enum Mode {
        FULL, PSEUDONYMOUS, CONTEXTUAL, SAFE, KIDS;

        /**
         * Whether a break in this mode may call any bidder but the own book.
         */
        public boolean allowsThirdParties() {
            return this != SAFE && this != KIDS;
        }
    }

    private static final Set<String> MODES = Set.of("FULL", "PSEUDONYMOUS", "CONTEXTUAL", "SAFE", "KIDS");

    /**
     * Reads a {@code /policy} response body.
     *
     * @throws IllegalArgumentException when it is not schema version 1 or a field has the wrong shape
     */
    public static PolicyPatch parse(JsonNode answer) {
        if (answer == null || !answer.isObject() || answer.path("v").asInt(-1) != VERSION) {
            throw new IllegalArgumentException("unknown schema version " + (answer != null ? answer.path("v") : null));
        }
        final String mode = answer.path("mode").asText("");
        if (!MODES.contains(mode)) {
            throw new IllegalArgumentException("unknown mode " + mode);
        }
        final ObjectNode bidders = optionalObject(answer, "bidders");
        if (bidders != null) {
            bidders.properties().forEach(entry -> require(entry.getValue().isObject(), "bidders." + entry.getKey()));
        }
        final List<String> fpdBidders = new ArrayList<>();
        final JsonNode fpd = answer.path("fpd").path("bidders");
        if (!fpd.isMissingNode() && !fpd.isNull()) {
            require(fpd.isArray(), "fpd.bidders");
            fpd.forEach(node -> {
                require(node.isTextual(), "fpd.bidders[]");
                fpdBidders.add(node.asText());
            });
        }
        final JsonNode deals = answer.path("deals");
        require(deals.isMissingNode() || deals.isNull() || deals.isArray(), "deals");
        final JsonNode enr = answer.path("enr");
        require(enr.isMissingNode() || enr.isNull() || enr.isInt() && (enr.asInt() == 0 || enr.asInt() == 1), "enr");
        final JsonNode decisionId = answer.path("decision_id");
        require(decisionId.isMissingNode() || decisionId.isTextual(), "decision_id");
        return new PolicyPatch(
                decisionId.asText(null),
                Mode.valueOf(mode),
                answer.path("reason").asText(null),
                bidders,
                optionalObject(answer, "regs"),
                optionalObject(answer, "device"),
                optionalObject(answer, "app_content"),
                optionalObject(answer, "user"),
                List.copyOf(fpdBidders),
                deals.isArray() ? (ArrayNode) deals : null,
                enr.asInt(0));
    }

    /**
     * Whether applying the patch changes the request at all.
     */
    public boolean changesRequest() {
        return !addedBidders().isEmpty() || regs != null || device != null || appContent != null
                || mode == Mode.FULL && user != null || !fpdBidders.isEmpty() || deals != null && !deals.isEmpty();
    }

    /**
     * The bidders the patch adds: none in {@code SAFE} and {@code KIDS} breaks.
     */
    public List<String> addedBidders() {
        if (bidders == null || !mode.allowsThirdParties()) {
            return List.of();
        }
        return bidders.properties().stream().map(Map.Entry::getKey).toList();
    }

    /**
     * {@code request} with the patch applied.
     */
    public BidRequest apply(BidRequest request, ObjectMapper mapper) {
        final BidRequest.BidRequestBuilder builder = request.toBuilder()
                .imp(request.getImp().stream().map(imp -> patchImp(imp, mapper)).toList());
        if (regs != null) {
            builder.regs(merge(mapper, request.getRegs(), regs, Regs.class));
        }
        if (device != null) {
            builder.device(merge(mapper, request.getDevice(), device, Device.class));
        }
        if (appContent != null) {
            final App app = request.getApp() != null ? request.getApp() : App.builder().build();
            builder.app(app.toBuilder().content(merge(mapper, app.getContent(), appContent, Content.class)).build());
        }
        if (mode == Mode.FULL && user != null) {
            builder.user(merge(mapper, request.getUser(), user, User.class));
        }
        if (!fpdBidders.isEmpty()) {
            final ObjectNode ext = request.getExt() != null ? mapper.valueToTree(request.getExt()) : mapper.createObjectNode();
            child(child(ext, "prebid"), "data").set("bidders", mapper.valueToTree(fpdBidders));
            builder.ext(mapper.convertValue(ext, ExtRequest.class));
        }
        return builder.build();
    }

    private Imp patchImp(Imp imp, ObjectMapper mapper) {
        final Imp.ImpBuilder builder = imp.toBuilder();
        final List<String> added = addedBidders();
        if (!added.isEmpty()) {
            final ObjectNode ext = imp.getExt() != null ? imp.getExt().deepCopy() : mapper.createObjectNode();
            final ObjectNode bidder = child(child(ext, "prebid"), "bidder");
            for (String name : added) {
                if (!bidder.has(name)) {
                    bidder.set(name, bidders.get(name).deepCopy());
                }
            }
            builder.ext(ext);
        }
        if (deals != null && !deals.isEmpty()) {
            final ObjectNode pmp = imp.getPmp() != null ? mapper.valueToTree(imp.getPmp()) : mapper.createObjectNode();
            final ArrayNode merged = pmp.get("deals") instanceof ArrayNode existing ? existing : pmp.putArray("deals");
            deals.forEach(deal -> merged.add(deal.deepCopy()));
            builder.pmp(mapper.convertValue(pmp, Pmp.class));
        }
        return builder.build();
    }

    /**
     * {@code current} with every field of {@code patch} set over it; nested objects of the patch replace whole
     * objects except {@code ext}, whose fields are set one by one.
     */
    private static <T> T merge(ObjectMapper mapper, T current, ObjectNode patch, Class<T> type) {
        final ObjectNode tree = current != null ? mapper.valueToTree(current) : mapper.createObjectNode();
        for (Map.Entry<String, JsonNode> field : patch.properties()) {
            if ("ext".equals(field.getKey()) && field.getValue().isObject() && tree.path("ext").isObject()) {
                ((ObjectNode) tree.get("ext")).setAll((ObjectNode) field.getValue().deepCopy());
            } else {
                tree.set(field.getKey(), field.getValue().deepCopy());
            }
        }
        return mapper.convertValue(tree, type);
    }

    private static ObjectNode optionalObject(JsonNode answer, String field) {
        final JsonNode node = answer.path(field);
        if (node.isMissingNode() || node.isNull()) {
            return null;
        }
        require(node.isObject(), field);
        return (ObjectNode) node;
    }

    private static ObjectNode child(ObjectNode parent, String name) {
        return parent.get(name) instanceof ObjectNode object ? object : parent.putObject(name);
    }

    private static void require(boolean condition, String field) {
        if (!condition) {
            throw new IllegalArgumentException("malformed " + field);
        }
    }
}
