package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.BidRequest;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.request.Video;
import org.junit.jupiter.api.Test;
import org.prebid.server.proto.openrtb.ext.request.ExtRequest;
import org.prebid.server.proto.openrtb.ext.request.ExtRequestPrebid;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;

public class PodShapeTest {

    private final PodShape target = new PodShape(30, 120);

    /** The one stored request as pbs_config renders it, with MediaTailor's {@code pod_s}. */
    static BidRequest stored(String podSeconds) {
        final ObjectNode fast = MAPPER.createObjectNode().put(PodShape.SLOT_FIELD, 30);
        if (podSeconds != null) {
            fast.put(PodShape.POD_FIELD, podSeconds);
        }
        final ExtRequest ext = ExtRequest.of(ExtRequestPrebid.builder().build());
        ext.addProperty(PodShape.EXT, fast);
        ext.addProperty("other", MAPPER.createObjectNode().put("kept", true));
        final Imp template = Imp.builder()
                .id("s1")
                .video(Video.builder().mimes(List.of("application/x-mpegURL")).podid("1").poddur(30)
                        .rqddurs(List.of(30)).slotinpod(0).build())
                .ext(MAPPER.createObjectNode().set("prebid", MAPPER.createObjectNode()
                        .set("bidder", MAPPER.createObjectNode().set("ownbook", MAPPER.createObjectNode()))))
                .build();
        return BidRequest.builder().id("r").imp(List.of(template)).tmax(500L).ext(ext).build();
    }

    @Test
    public void aBreaksLengthIsItsWholeSlotsEachWithThePodsDuration() {
        final int[][] cases = {{30, 1}, {31, 1}, {48, 1}, {59, 1}, {60, 2}, {84, 2}, {90, 3}, {108, 3}, {119, 3},
            {120, 4}};
        for (int[] each : cases) {
            assertShaped(String.valueOf(each[0]), each[1]);
        }
    }

    private void assertShaped(String seconds, int slots) {
        final PodShape.Result result = target.apply(stored(seconds));

        assertThat(result.refused()).isNull();
        assertThat(result.podSeconds()).isEqualTo(Integer.parseInt(seconds));
        assertThat(result.slots()).isEqualTo(slots);
        final List<Imp> imps = result.request().getImp();
        assertThat(imps).extracting(Imp::getId)
                .containsExactlyElementsOf(java.util.stream.IntStream.rangeClosed(1, slots).mapToObj(n -> "s" + n)
                        .toList());
        assertThat(imps).allSatisfy(imp -> {
            assertThat(imp.getVideo().getPoddur()).isEqualTo(Integer.parseInt(seconds));
            assertThat(imp.getVideo().getPodid()).isEqualTo("1");
            assertThat(imp.getVideo().getRqddurs()).containsExactly(30);
            assertThat(imp.getExt().at("/prebid/bidder/ownbook").isObject()).isTrue();
        });
    }

    @Test
    public void slotinpodMarksTheFirstAndLastSlotAndASingleSlotIsZero() {
        assertThat(positions("120")).containsExactly(1, 0, 0, -1);
        assertThat(positions("60")).containsExactly(1, -1);
        assertThat(positions("48")).containsExactly(0);
    }

    @Test
    public void extFastIsRemovedSoNoBidderSeesItAndHandedOn() {
        final PodShape.Result result = target.apply(stored("90"));

        final ExtRequest ext = result.request().getExt();
        assertThat(ext.getProperty(PodShape.EXT)).isNull();
        assertThat(ext.getProperty("other").get("kept").asBoolean()).isTrue();
        assertThat(ext.getPrebid()).isNotNull();
        assertThat(result.fast().get(PodShape.POD_FIELD).textValue()).isEqualTo("90");
    }

    @Test
    public void aLengthOutOfBoundsIsRefused() {
        for (String seconds : List.of("0", "20", "29", "121", "300", "999")) {
            final BidRequest request = stored(seconds);
            final PodShape.Result result = target.apply(request);

            assertThat(result.refused()).as(seconds).isEqualTo("pod_s %s outside 30-120 s".formatted(seconds));
            assertThat(result.refusal()).isEqualTo(PodShape.Refusal.OUT_OF_RANGE);
            assertThat(result.shaped()).isFalse();
            assertThat(result.request()).isSameAs(request);
        }
    }

    @Test
    public void aLengthThatIsNotWholeSecondsIsRefusedWithoutLoggingIt() {
        for (String seconds : List.of("", "1000", "48.5", "abc", "-30", " 48", "48 ", "4 8")) {
            final PodShape.Result result = target.apply(stored(seconds));

            assertThat(result.refused()).as(seconds)
                    .isEqualTo("pod_s is not whole seconds (length %d)".formatted(seconds.length()));
            assertThat(result.refusal()).isEqualTo(PodShape.Refusal.UNREADABLE);
            assertThat(result.shaped()).isFalse();
        }
    }

    @Test
    public void aMissingLengthIsRefused() {
        assertThat(target.apply(stored(null)).refused()).isEqualTo("pod_s is not whole seconds (length -1)");
    }

    @Test
    public void aRequestThatIsNotAModulePodPassesUnchanged() {
        final BidRequest plain = BidRequest.builder().id("r").imp(List.of(Imp.builder().id("1").build())).build();

        final PodShape.Result result = target.apply(plain);

        assertThat(result.request()).isSameAs(plain);
        assertThat(result.shaped()).isFalse();
        assertThat(result.refused()).isNull();
    }

    @Test
    public void theBoundsMustBeAnInterval() {
        assertThatThrownBy(() -> new PodShape(0, 120)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new PodShape(60, 30)).isInstanceOf(IllegalArgumentException.class);
    }

    private List<Integer> positions(String seconds) {
        return target.apply(stored(seconds)).request().getImp().stream().map(imp -> imp.getVideo().getSlotinpod())
                .toList();
    }
}
