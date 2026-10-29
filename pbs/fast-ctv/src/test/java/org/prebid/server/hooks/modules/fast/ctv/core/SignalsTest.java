package org.prebid.server.hooks.modules.fast.ctv.core;

import org.junit.jupiter.api.Test;
import org.prebid.server.model.CaseInsensitiveMultiMap;
import org.prebid.server.model.HttpRequestContext;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.DECISION;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.TICKET;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.mediaTailorRequest;

public class SignalsTest {

    @Test
    public void theTicketKeepsOnlyItsAlphabetAndAtMost22Characters() {
        assertThat(Signals.of(mediaTailorRequest(TICKET)).ticket()).isEqualTo(TICKET);
        final Map<String, String> forwarded = Map.of(
                "", "",
                "\"", "",
                "a\\b", "ab",
                "x".repeat(30), "x".repeat(22),
                "has space", "hasspace",
                "ü", "",
                "abc", "abc");
        forwarded.forEach((sent, kept) -> assertThat(Signals.of(mediaTailorRequest(sent)).ticket()).as(sent)
                .isEqualTo(kept));
    }

    @Test
    public void theBreakComesFromTheQuery() {
        final Signals signals = Signals.of(mediaTailorRequest(TICKET));

        assertThat(signals.evid()).isEqualTo("820461826");
        assertThat(signals.upid()).isEqualTo("tears-of-steel");
        assertThat(signals.durMs()).isEqualTo(60060);
        assertThat(signals.buffet()).isTrue();
        assertThat(signals.probe()).isFalse();
        assertThat(signals.decisionId()).isEqualTo(DECISION);
        assertThat(signals.problem()).isNull();
    }

    @Test
    public void probeBuffetAndContextHeaders() {
        final Signals signals = Signals.of(mediaTailorRequest(TICKET, "X-Fast-Probe", "1", "X-Fast-Buffet", "0",
                "x-fast-ctx", "c".repeat(Signals.MAX_CONTEXT)));
        final Signals tooLong = Signals.of(mediaTailorRequest(TICKET, "x-fast-ctx", "c".repeat(Signals.MAX_CONTEXT + 1)));

        assertThat(signals.probe()).isTrue();
        assertThat(signals.buffet()).isFalse();
        assertThat(signals.ctx()).hasSize(Signals.MAX_CONTEXT);
        assertThat(tooLong.ctx()).isEmpty();
        assertThat(tooLong.ctxLength()).isEqualTo(Signals.MAX_CONTEXT + 1);
    }

    @Test
    public void optionalValuesLongerThanTheContractAllowsAreDropped() {
        final Signals signals = Signals.of(HttpRequestContext.builder()
                .headers(CaseInsensitiveMultiMap.builder()
                        .add("User-Agent", "u".repeat(Signals.MAX_USER_AGENT + 1))
                        .add("X-Forwarded-For", "1".repeat(Signals.MAX_XFF + 1))
                        .add("CloudFront-Viewer-Address", "2".repeat(Signals.MAX_CFVA + 1))
                        .add("X-Fast-Fv", "r0001;drop").build())
                .queryParams(CaseInsensitiveMultiMap.builder().add("upid", "p".repeat(Signals.MAX_UPID + 1)).build())
                .build());

        assertThat(signals.userAgent()).isEmpty();
        assertThat(signals.xff()).isEmpty();
        assertThat(signals.cfva()).isEmpty();
        assertThat(signals.fv()).isEmpty();
        assertThat(signals.upid()).isEmpty();
    }

    @Test
    public void aBreakWithoutItsIdentifiersIsNamedAsAProblem() {
        assertThat(Signals.of(mediaTailorRequest(TICKET)).problem()).isNull();
        assertThat(Signals.of(null).problem()).isEqualTo("sid");
        final Signals noDuration = Signals.of(HttpRequestContext.builder()
                .headers(CaseInsensitiveMultiMap.builder().add("X-Fast-Sid", "s-1").add("X-Fast-Pc", "fastm-p-a")
                        .build())
                .queryParams(CaseInsensitiveMultiMap.builder().add("evid", "42").add("dur", "sixty").build())
                .build());
        final Signals badSid = Signals.of(HttpRequestContext.builder()
                .headers(CaseInsensitiveMultiMap.builder().add("X-Fast-Sid", "s 1").add("X-Fast-Pc", "fastm-p-a")
                        .build())
                .queryParams(CaseInsensitiveMultiMap.builder().add("evid", "42").add("dur", "60060").build())
                .build());
        final Signals badEvid = Signals.of(HttpRequestContext.builder()
                .headers(CaseInsensitiveMultiMap.builder().add("X-Fast-Sid", "s-1").add("X-Fast-Pc", "fastm-p-a")
                        .build())
                .queryParams(CaseInsensitiveMultiMap.builder().add("evid", "[scte.event_id]").add("dur", "60060")
                        .build())
                .build());

        assertThat(noDuration.durMs()).isNull();
        assertThat(noDuration.problem()).isEqualTo("dur_ms");
        assertThat(badSid.problem()).isEqualTo("sid");
        assertThat(badEvid.problem()).isEqualTo("evid");
    }

    @Test
    public void ourTrackerEncodesItsValues() {
        final TrackerTemplate tracker = new TrackerTemplate(
                "https://cdn.example/ads/b.gif?d={sid}.{evid}&c={crid}&e={event}&enr={enr}");

        assertThat(tracker.impression("s 1", "42", "a&b", 1))
                .isEqualTo("https://cdn.example/ads/b.gif?d=s+1.42&c=a%26b&e=impression&enr=1");
        assertThatThrownBy(() -> new TrackerTemplate("https://cdn.example/ads/b.gif?d={sid}"))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new TrackerTemplate("ftp://cdn.example/{crid}"))
                .isInstanceOf(IllegalArgumentException.class);
    }
}
