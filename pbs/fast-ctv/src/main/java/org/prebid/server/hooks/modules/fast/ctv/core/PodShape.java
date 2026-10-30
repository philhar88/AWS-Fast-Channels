package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.JsonNode;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Video;
import org.prebid.server.proto.openrtb.ext.request.ExtRequest;

import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;
import java.util.stream.IntStream;

/**
 * The break's pod, built from the one stored request and the break's length.
 * <p>
 * MediaTailor's body carries the break's whole seconds as a string, {@code ext.fast.pod_s}
 * ({@code {{session.avail_duration_secs}}}: a missing value leaves the body valid JSON, where an unquoted number would
 * not, and MediaTailor drops an invalid body silently). The stored request holds one template slot and the slot
 * length, {@code ext.fast.slot_s}. The pod is {@code pod_s / slot_s} copies of the template, {@code s1} to
 * {@code sN}, each with {@code poddur} = {@code pod_s} and {@code slotinpod} 1 for the first, -1 for the last, 0
 * between (0 for a single slot): the slots a per-length stored request used to hold. {@code ext.fast} is then removed,
 * so no bidder request carries it.
 * <p>
 * A request without {@code ext.fast.slot_s} is not one of the module's pods and passes unchanged. One whose
 * {@code pod_s} is missing, not whole seconds ({@code ^[0-9]{1,3}$}), outside the configured bounds, or shorter than a
 * slot is refused: the auction is rejected and the break airs the slate.
 */
public final class PodShape {

    public static final String EXT = "fast";
    public static final String POD_FIELD = "pod_s";
    public static final String SLOT_FIELD = "slot_s";

    private static final Pattern WHOLE_SECONDS = Pattern.compile("[0-9]{1,3}");

    private final int minSeconds;
    private final int maxSeconds;

    /**
     * What shaping did.
     *
     * @param request    the request to auction (the shaped pod, or the original one when it is not a module pod)
     * @param podSeconds the pod's length, or null when not shaped
     * @param slots      the pod's slots, or 0 when not shaped
     * @param fast       the request's {@code ext.fast} before it was removed (the ad-server mode's ask), or null
     * @param refused    why the pod was refused, or null
     */
    public record Result(BidRequest request, Integer podSeconds, int slots, JsonNode fast, String refused) {

        public boolean shaped() {
            return podSeconds != null;
        }
    }

    public PodShape(int minSeconds, int maxSeconds) {
        if (minSeconds < 1 || maxSeconds < minSeconds) {
            throw new IllegalArgumentException("pod bounds %d-%d s".formatted(minSeconds, maxSeconds));
        }
        this.minSeconds = minSeconds;
        this.maxSeconds = maxSeconds;
    }

    public Result apply(BidRequest request) {
        final ExtRequest ext = request.getExt();
        final JsonNode fast = ext != null ? ext.getProperty(EXT) : null;
        final JsonNode slot = fast != null ? fast.get(SLOT_FIELD) : null;
        if (slot == null || !slot.canConvertToInt() || slot.asInt() < 1) {
            return new Result(request, null, 0, fast, null);
        }
        final JsonNode pod = fast.get(POD_FIELD);
        final String text = pod != null && pod.isTextual() ? pod.textValue() : null;
        if (text == null || !WHOLE_SECONDS.matcher(text).matches()) {
            return refused(request, fast, "pod_s is not whole seconds (length %d)".formatted(
                    text != null ? text.length() : -1));
        }
        final int seconds = Integer.parseInt(text);
        final int slotSeconds = slot.asInt();
        if (seconds < minSeconds || seconds > maxSeconds || seconds < slotSeconds) {
            return refused(request, fast, "pod_s %d outside %d-%d s".formatted(seconds, minSeconds, maxSeconds));
        }
        final List<Imp> imps = request.getImp();
        if (imps == null || imps.isEmpty() || imps.getFirst().getVideo() == null) {
            return refused(request, fast, "no template slot");
        }
        final Imp template = imps.getFirst();
        final int count = seconds / slotSeconds;
        final List<Imp> slots = IntStream.rangeClosed(1, count)
                .mapToObj(index -> slot(template, index, count, seconds))
                .toList();
        final BidRequest shaped = request.toBuilder().imp(slots).ext(withoutFast(ext)).build();
        return new Result(shaped, seconds, count, fast, null);
    }

    private static Imp slot(Imp template, int index, int count, int seconds) {
        final int position = count == 1 ? 0 : index == 1 ? 1 : index == count ? -1 : 0;
        final Video video = template.getVideo().toBuilder().poddur(seconds).slotinpod(position).build();
        return template.toBuilder().id("s" + index).video(video).build();
    }

    private static ExtRequest withoutFast(ExtRequest ext) {
        final ExtRequest copy = ExtRequest.of(ext.getPrebid());
        for (Map.Entry<String, JsonNode> property : ext.getProperties().entrySet()) {
            if (!EXT.equals(property.getKey())) {
                copy.addProperty(property.getKey(), property.getValue());
            }
        }
        return copy;
    }

    private static Result refused(BidRequest request, JsonNode fast, String why) {
        return new Result(request, null, 0, fast, why);
    }
}
