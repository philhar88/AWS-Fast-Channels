package org.prebid.server.hooks.modules.fast.ctv.core;

import org.junit.jupiter.api.Test;
import org.prebid.server.model.CaseInsensitiveMultiMap;
import org.prebid.server.model.HttpRequestContext;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.TICKET;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.mediaTailorRequest;

public class SignalsTest {

    @Test
    public void aWellFormedTicketIsKeptAndAnythingElseIsDropped() {
        assertThat(Signals.of(mediaTailorRequest(TICKET)).ticket()).isEqualTo(TICKET);
        for (String bad : List.of("", "\"", "a\\b", "x".repeat(23), "has space", "ü")) {
            assertThat(Signals.of(mediaTailorRequest(bad)).ticket()).as(bad).isEmpty();
        }
    }

    @Test
    public void theBreakComesFromTheQuery() {
        final Signals signals = Signals.of(mediaTailorRequest(TICKET));

        assertThat(signals.evid()).isEqualTo("820461826");
        assertThat(signals.upid()).isEqualTo("tears-of-steel");
        assertThat(signals.durMs()).isEqualTo(60060);
        assertThat(signals.buffet()).isTrue();
        assertThat(signals.probe()).isFalse();
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
    public void aRequestWithoutHeadersOrQueryHasEmptySignals() {
        final Signals signals = Signals.of(HttpRequestContext.builder().headers(CaseInsensitiveMultiMap.empty())
                .queryParams(CaseInsensitiveMultiMap.builder().add("dur", "sixty").build()).build());

        assertThat(signals.sid()).isEmpty();
        assertThat(signals.durMs()).isNull();
        assertThat(Signals.of(null).ticket()).isEmpty();
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
