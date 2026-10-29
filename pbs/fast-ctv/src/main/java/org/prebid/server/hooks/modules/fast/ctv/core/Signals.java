package org.prebid.server.hooks.modules.fast.ctv.core;

import org.prebid.server.model.CaseInsensitiveMultiMap;
import org.prebid.server.model.HttpRequestContext;

import java.util.Objects;
import java.util.regex.Pattern;

/**
 * What one MediaTailor ad request tells the module, read from its headers and query.
 * <p>
 * MediaTailor's POST carries the playback ticket and the session in header templates ({@code X-Fast-T},
 * {@code X-Fast-Sid}), the calling playback configuration and its Function release as header constants
 * ({@code X-Fast-Pc}, {@code X-Fast-Fv}, {@code X-Fast-Probe}, {@code X-Fast-Buffet}) and the break in the query
 * ({@code evid}, {@code upid}, {@code dur} in milliseconds). Every value is untrusted: the ticket is kept only when it
 * has the ticket's shape, and the content context only up to {@link #MAX_CONTEXT} characters.
 *
 * @param ticket     the playback ticket, or empty when it is missing or malformed
 * @param sid        MediaTailor's session id, or empty
 * @param pc         the playback configuration, or empty
 * @param fv         the playback configuration's Function release ({@code rNNNN}), or empty
 * @param probe      a probe session ({@code X-Fast-Probe: 1}): house only
 * @param buffet     whether to add standalone ads for ad buffet ({@code X-Fast-Buffet: 0} turns them off)
 * @param evid       the break's SCTE-35 event id, or empty
 * @param upid       the break's segmentation UPID, or empty
 * @param durMs      the avail duration in milliseconds, or null when absent or not a number
 * @param userAgent  the viewer's user agent, as MediaTailor forwards it
 * @param xff        {@code X-Forwarded-For} as received
 * @param cfva       {@code CloudFront-Viewer-Address}, or empty
 * @param ctx        the content context header ({@code x-fast-ctx}), or empty when absent or too long
 * @param ctxLength  the content context header's length as received (0 when absent)
 */
public record Signals(String ticket, String sid, String pc, String fv, boolean probe, boolean buffet,
                      String evid, String upid, Integer durMs, String userAgent, String xff, String cfva,
                      String ctx, int ctxLength) {

    public static final int MAX_CONTEXT = 1000;

    private static final Pattern TICKET = Pattern.compile("[A-Za-z0-9_-]{1,22}");
    private static final Pattern DIGITS = Pattern.compile("\\d{1,9}");

    public static Signals of(HttpRequestContext http) {
        final CaseInsensitiveMultiMap headers = http != null ? http.getHeaders() : CaseInsensitiveMultiMap.empty();
        final CaseInsensitiveMultiMap query = http != null ? http.getQueryParams() : CaseInsensitiveMultiMap.empty();
        final String ticket = value(headers, "X-Fast-T");
        final String ctx = value(headers, "x-fast-ctx");
        final String dur = value(query, "dur");
        return new Signals(
                TICKET.matcher(ticket).matches() ? ticket : "",
                value(headers, "X-Fast-Sid"),
                value(headers, "X-Fast-Pc"),
                value(headers, "X-Fast-Fv"),
                "1".equals(value(headers, "X-Fast-Probe")),
                !"0".equals(value(headers, "X-Fast-Buffet")),
                value(query, "evid"),
                value(query, "upid"),
                DIGITS.matcher(dur).matches() ? Integer.valueOf(dur) : null,
                value(headers, "User-Agent"),
                value(headers, "X-Forwarded-For"),
                value(headers, "CloudFront-Viewer-Address"),
                ctx.length() <= MAX_CONTEXT ? ctx : "",
                ctx.length());
    }

    /**
     * The id of this break's decision when the policy did not name one: {@code <sid>.<evid>}.
     */
    public String defaultDecisionId() {
        return sid + "." + evid;
    }

    private static String value(CaseInsensitiveMultiMap map, String name) {
        return Objects.requireNonNullElse(map.get(name), "");
    }
}
