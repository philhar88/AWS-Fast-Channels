package org.prebid.server.hooks.modules.fast.ctv.core;

import org.junit.jupiter.api.Test;

import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.MAPPER;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.pod;
import static org.prebid.server.hooks.modules.fast.ctv.Fixtures.vast;

public class HousePodTest {

    @Test
    public void theConfigurationIsAJsonArrayOfHouseAds() {
        final String json = MAPPER.createArrayNode().add(MAPPER.createObjectNode().put("crid", "house1")
                .put("adm", vast("house1", null)).put("dur", 30).put("w", 1920).put("h", 1080)).toString();

        final HousePod pod = HousePod.parse(json, MAPPER);

        assertThat(pod.isEmpty()).isFalse();
        assertThat(HousePod.parse("[]", MAPPER).isEmpty()).isTrue();
        assertThat(HousePod.parse(null, MAPPER)).isSameAs(HousePod.NONE);
        assertThat(HousePod.parse(" ", MAPPER)).isSameAs(HousePod.NONE);
    }

    @Test
    public void aMalformedConfigurationStopsTheModule() {
        assertThatThrownBy(() -> HousePod.parse("{\"crid\":\"x\"}", MAPPER))
                .isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> HousePod.parse("[{\"crid\":\"x\",\"dur\":30}]", MAPPER))
                .isInstanceOf(IllegalArgumentException.class).hasMessageContaining("crid, adm and dur");
    }

    @Test
    public void everyAdIsAHouseCandidateInEverySlot() {
        final HousePod pod = new HousePod(List.of(new HousePod.Ad("house1", vast("house1", null), 30, 1920, 1080),
                new HousePod.Ad("house2", vast("house2", null), 30, 1920, 1080)));

        final List<Candidate> candidates = pod.candidates(pod(2).getImp(), new PodRenderer(), MAPPER);

        assertThat(candidates).extracting(Candidate::ref).containsExactly("h0", "h1", "h2", "h3");
        assertThat(candidates).extracting(Candidate::impid).containsExactly("s1", "s1", "s2", "s2");
        assertThat(candidates).allMatch(c -> c.house() && c.usable() && HousePod.SEAT.equals(c.seat())
                && c.bid().getPrice().signum() == 0 && c.vastDurMs() == 30_030);
    }
}
