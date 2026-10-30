package org.prebid.server.hooks.modules.fast.ctv.core;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import com.iab.openrtb.request.Imp;
import com.iab.openrtb.response.Bid;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/**
 * The kit's house ads from the module's configuration ({@code hooks.fast-ctv.house-ads}, a JSON array of
 * {@code {crid, adm, dur, w, h}}): the pod of last resort when a break would otherwise be empty because the Pod
 * Decision Service could not be asked: {@code /policy} unavailable and the own book silent, or {@code /select}
 * unavailable and the auction without a house ad. They are offered in every slot, as the own book offers its house
 * ads, and are never sold.
 */
public final class HousePod {

    /** The seat of a configured house ad (its {@code seat:crid} label in the module's log line). */
    public static final String SEAT = "fast-ctv";

    public static final HousePod NONE = new HousePod(List.of());

    private final List<Ad> ads;

    /**
     * One configured house ad.
     *
     * @param crid its creative id ({@code house1}, ...)
     * @param adm  an inline VAST 4.2 ad without an impression of its own
     * @param dur  its duration, whole seconds
     * @param w    width
     * @param h    height
     */
    public record Ad(String crid, String adm, int dur, int w, int h) {
    }

    public HousePod(List<Ad> ads) {
        this.ads = List.copyOf(Objects.requireNonNull(ads));
    }

    /**
     * The configured ads of {@code json} ({@code null} or blank: none).
     *
     * @throws IllegalArgumentException when it is not the array the configuration renders
     */
    public static HousePod parse(String json, ObjectMapper mapper) {
        if (json == null || json.isBlank()) {
            return NONE;
        }
        try {
            final Ad[] ads = mapper.readValue(json, Ad[].class);
            for (Ad ad : ads) {
                if (ad == null || ad.crid() == null || ad.crid().isBlank() || ad.adm() == null || ad.dur() <= 0) {
                    throw new IllegalArgumentException("hooks.fast-ctv.house-ads: every ad needs crid, adm and dur");
                }
            }
            return new HousePod(List.of(ads));
        } catch (JsonProcessingException e) {
            throw new IllegalArgumentException("hooks.fast-ctv.house-ads is not a JSON array of house ads", e);
        }
    }

    public boolean isEmpty() {
        return ads.isEmpty();
    }

    /**
     * Every configured ad in every slot of {@code imps}, as house candidates with references {@code h0, h1, ...}
     * (the auction's are {@code b0, b1, ...}).
     */
    public List<Candidate> candidates(List<Imp> imps, PodRenderer renderer, ObjectMapper mapper) {
        final List<Candidate> candidates = new ArrayList<>();
        for (Imp imp : imps) {
            for (Ad ad : ads) {
                final ObjectNode ext = mapper.createObjectNode();
                ext.putObject("fast").put("house", true);
                final Bid bid = Bid.builder()
                        .id("house-" + ad.crid() + "-" + imp.getId())
                        .impid(imp.getId())
                        .price(BigDecimal.ZERO)
                        .adm(ad.adm())
                        .crid(ad.crid())
                        .dur(ad.dur())
                        .w(ad.w())
                        .h(ad.h())
                        .ext(ext)
                        .build();
                candidates.add(renderer.parse("h" + candidates.size(), SEAT, bid));
            }
        }
        return candidates;
    }
}
