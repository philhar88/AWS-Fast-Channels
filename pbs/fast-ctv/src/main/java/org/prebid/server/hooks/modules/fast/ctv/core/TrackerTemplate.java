package org.prebid.server.hooks.modules.fast.ctv.core;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Objects;

/**
 * The module's own impression tracker: MediaTailor drops an inline ad that has no {@code <Impression>}, without a
 * skip reason, and our tracker is how delivery is counted. Every rendered ad carries ours: the Pod Decision Service's
 * trackers when {@code /select} lists them (they know the break's content-enrichment arm), else this template's,
 * with {@code enr} 0 (the house fallback, or an answer that lists none for the ad).
 * <p>
 * The template is a URL with the placeholders {@code {sid}}, {@code {evid}}, {@code {crid}}, {@code {event}} and
 * {@code {enr}}, for example {@code https://cdn.example/ads/b.gif?d={sid}.{evid}&c={crid}&e={event}&enr={enr}}. Each
 * value is URL-encoded.
 */
public class TrackerTemplate {

    public static final String IMPRESSION = "impression";

    private final String template;

    public TrackerTemplate(String template) {
        Objects.requireNonNull(template, "hooks.fast-ctv.tracker-template");
        if (!template.contains("{crid}")) {
            throw new IllegalArgumentException("hooks.fast-ctv.tracker-template must contain {crid}: " + template);
        }
        this.template = template;
        Urls.require(impression("s", "1", "c", 0), "hooks.fast-ctv.tracker-template");
    }

    /**
     * The impression tracker URL of one ad.
     */
    public String impression(String sid, String evid, String crid, int enr) {
        return template.replace("{sid}", encode(sid))
                .replace("{evid}", encode(evid))
                .replace("{crid}", encode(crid))
                .replace("{event}", IMPRESSION)
                .replace("{enr}", Integer.toString(enr));
    }

    private static String encode(String value) {
        return URLEncoder.encode(Objects.requireNonNullElse(value, ""), StandardCharsets.UTF_8);
    }
}
