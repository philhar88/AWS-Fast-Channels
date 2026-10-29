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
 * The Pod Decision Service's answer to {@code /policy} ({@code contracts/pds-policy.schema.json}, version 1), and how
 * it changes the bid request.
 * <p>
 * The module applies the patch verbatim and only adds: the returned bidders join every imp beside the stored
 * request's own book, which is never removed. {@code regs}, {@code device} and {@code app_content} fields are set
 * over the request's, a null one removing the request's value (a null {@code device.ip} removes the address Prebid
 * Server took from the client-settable {@code X-Forwarded-For}); {@code user} only in {@code FULL} mode; first-party
 * data is scoped to {@code fpd.bidders}; {@code deals} join every imp's private marketplace. {@code SAFE} and
 * {@code KIDS} breaks never gain a bidder, whatever the answer says. The contract is strict both ways: an answer
 * with an unknown field, a field of the wrong shape or another break's {@code decision_id} is refused as a whole
 * ({@link #parse} throws), and the request stays as stored.
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
 */
public record PolicyPatch(String decisionId, Mode mode, String reason, ObjectNode bidders, ObjectNode regs,
                          ObjectNode device, ObjectNode appContent, ObjectNode user, List<String> fpdBidders,
                          ArrayNode deals) {

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

    private static final Set<String> FIELDS = Set.of("v", "decision_id", "mode", "reason", "bidders", "regs",
            "device", "app_content", "user", "fpd", "deals");
    private static final Set<String> MODES = Set.of("FULL", "PSEUDONYMOUS", "CONTEXTUAL", "SAFE", "KIDS");
    private static final Set<String> REASONS = Set.of("bound", "missing", "invalid", "unknown", "replay",
            "pc_mismatch", "expired_or_raced", "opted_out", "consent_unavailable", "probe", "kids");
    private static final Set<String> REGS = Set.of("coppa", "gpp", "gpp_sid");
    private static final Set<String> DEVICE = Set.of("ip", "ipv6", "devicetype");
    private static final Set<String> FPD = Set.of("bidders");
    private static final Set<String> DEAL = Set.of("id", "bidfloor", "at", "wseat");

    /**
     * Reads a {@code /policy} response body for the break {@code decisionId}.
     *
     * @throws IllegalArgumentException when it is not schema version 1, has a field the schema does not, a field of
     *                                  the wrong shape, or another decision id
     */
    public static PolicyPatch parse(JsonNode answer, String decisionId) {
        if (answer == null || !answer.isObject() || !answer.path("v").isInt() || answer.path("v").asInt() != VERSION) {
            throw new IllegalArgumentException("unknown schema version " + (answer != null ? answer.path("v") : null));
        }
        only(answer, FIELDS, "");
        final JsonNode id = answer.path("decision_id");
        require(id.isTextual() && id.asText().equals(decisionId), "decision_id");
        final String mode = answer.path("mode").asText("");
        require(answer.path("mode").isTextual() && MODES.contains(mode), "mode");
        require(answer.path("reason").isTextual() && REASONS.contains(answer.path("reason").asText()), "reason");

        final ObjectNode bidders = optionalObject(answer, "bidders");
        if (bidders != null) {
            bidders.properties().forEach(entry -> require(entry.getValue().isObject(), "bidders." + entry.getKey()));
        }
        final ObjectNode regs = optionalObject(answer, "regs");
        if (regs != null) {
            only(regs, REGS, "regs.");
            final JsonNode coppa = regs.path("coppa");
            require(coppa.isMissingNode() || coppa.isInt() && (coppa.asInt() == 0 || coppa.asInt() == 1),
                    "regs.coppa");
            require(nullOr(regs.path("gpp"), JsonNode::isTextual), "regs.gpp");
            require(nullOr(regs.path("gpp_sid"), node -> allOf(node, JsonNode::isInt)), "regs.gpp_sid");
        }
        final ObjectNode device = optionalObject(answer, "device");
        if (device != null) {
            only(device, DEVICE, "device.");
            require(nullOr(device.path("ip"), JsonNode::isTextual), "device.ip");
            require(nullOr(device.path("ipv6"), JsonNode::isTextual), "device.ipv6");
            require(nullOr(device.path("devicetype"), node -> node.isInt() && node.asInt() >= 1 && node.asInt() <= 8),
                    "device.devicetype");
        }
        final List<String> fpdBidders = new ArrayList<>();
        final ObjectNode fpd = optionalObject(answer, "fpd");
        if (fpd != null) {
            only(fpd, FPD, "fpd.");
            final JsonNode names = fpd.path("bidders");
            require(names.isMissingNode() || allOf(names, JsonNode::isTextual), "fpd.bidders");
            names.forEach(node -> fpdBidders.add(node.asText()));
        }
        final JsonNode deals = answer.path("deals");
        require(deals.isMissingNode() || deals.isArray(), "deals");
        deals.forEach(deal -> {
            require(deal.isObject(), "deals[]");
            only(deal, DEAL, "deals[].");
            require(deal.path("id").isTextual() && !deal.path("id").asText().isEmpty(), "deals[].id");
            require(nullOr(deal.path("bidfloor"), node -> node.isNumber() && node.asDouble() >= 0), "deals[].bidfloor");
            require(nullOr(deal.path("at"), node -> node.isInt() && node.asInt() >= 1 && node.asInt() <= 3),
                    "deals[].at");
            require(deal.path("wseat").isMissingNode() || allOf(deal.path("wseat"), JsonNode::isTextual),
                    "deals[].wseat");
        });
        return new PolicyPatch(
                id.asText(),
                Mode.valueOf(mode),
                answer.path("reason").asText(),
                bidders,
                regs,
                device,
                optionalObject(answer, "app_content"),
                optionalObject(answer, "user"),
                List.copyOf(fpdBidders),
                deals.isArray() ? (ArrayNode) deals : null);
    }

    /**
     * Whether applying the patch changes the request at all.
     */
    public boolean changesRequest() {
        return !addedBidders().isEmpty() || regs != null && !regs.isEmpty() || device != null && !device.isEmpty()
                || appContent != null || mode == Mode.FULL && user != null || !fpdBidders.isEmpty()
                || deals != null && !deals.isEmpty();
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
        if (regs != null && !regs.isEmpty()) {
            builder.regs(merge(mapper, request.getRegs(), regs, Regs.class));
        }
        if (device != null && !device.isEmpty()) {
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
            deals.forEach(deal -> merged.add(withoutNulls((ObjectNode) deal)));
            builder.pmp(mapper.convertValue(pmp, Pmp.class));
        }
        return builder.build();
    }

    /**
     * {@code current} with every field of {@code patch} set over it (a null removes the field); nested objects of
     * the patch replace whole objects except {@code ext}, whose fields are set one by one.
     */
    private static <T> T merge(ObjectMapper mapper, T current, ObjectNode patch, Class<T> type) {
        final ObjectNode tree = current != null ? mapper.valueToTree(current) : mapper.createObjectNode();
        for (Map.Entry<String, JsonNode> field : patch.properties()) {
            if (field.getValue().isNull()) {
                tree.remove(field.getKey());
            } else if ("ext".equals(field.getKey()) && field.getValue().isObject() && tree.path("ext").isObject()) {
                ((ObjectNode) tree.get("ext")).setAll((ObjectNode) field.getValue().deepCopy());
            } else {
                tree.set(field.getKey(), field.getValue().deepCopy());
            }
        }
        return mapper.convertValue(tree, type);
    }

    private static ObjectNode withoutNulls(ObjectNode node) {
        final ObjectNode copy = node.deepCopy();
        copy.properties().removeIf(field -> field.getValue().isNull());
        return copy;
    }

    private static ObjectNode optionalObject(JsonNode answer, String field) {
        final JsonNode node = answer.path(field);
        if (node.isMissingNode() || node.isNull()) {
            return null;
        }
        require(node.isObject(), field);
        return (ObjectNode) node;
    }

    private static void only(JsonNode node, Set<String> fields, String prefix) {
        node.fieldNames().forEachRemaining(name -> require(fields.contains(name), prefix + name + " (unknown field)"));
    }

    private static boolean nullOr(JsonNode node, java.util.function.Predicate<JsonNode> test) {
        return node.isMissingNode() || node.isNull() || test.test(node);
    }

    private static boolean allOf(JsonNode node, java.util.function.Predicate<JsonNode> test) {
        if (!node.isArray()) {
            return false;
        }
        for (JsonNode item : node) {
            if (!test.test(item)) {
                return false;
            }
        }
        return true;
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
