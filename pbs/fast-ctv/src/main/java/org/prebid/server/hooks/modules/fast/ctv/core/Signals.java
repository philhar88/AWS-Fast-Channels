package org.prebid.server.hooks.modules.fast.ctv.core;

import org.prebid.server.model.CaseInsensitiveMultiMap;
import org.prebid.server.model.HttpRequestContext;

import java.util.Objects;
import java.util.regex.Pattern;

/**
 * What one MediaTailor ad request tells the module, read from its headers and query, shaped as the Pod Decision
 * Service contract (version 1, {@code contracts/pds-policy.schema.json}) takes it.
 * <p>
 * MediaTailor's POST carries the playback ticket and the session in header templates ({@code X-Fast-T},
 * {@code X-Fast-Sid}), the calling playback configuration and its Function release as header constants
 * ({@code X-Fast-Pc}, {@code X-Fast-Fv}, {@code X-Fast-Probe}, {@code X-Fast-Buffet}) and the break in the query
 * ({@code evid}, {@code upid}, {@code dur} in milliseconds). Every value is untrusted. The ticket keeps only its
 * alphabet ({@code [A-Za-z0-9_-]}, at most 22 characters), so the service can tell a malformed ticket from a missing
 * one; an optional value longer than the contract allows is dropped. The session, configuration, event id and
 * duration identify the decision: when one is missing or malformed, {@link #problem()} names it and the module asks
 * the service nothing.
 *
 * @param ticket     the playback ticket's URL-safe characters, at most 22, or empty
 * @param sid        MediaTailor's session id, or empty
 * @param pc         the playback configuration, or empty
 * @param fv         the playback configuration's Function release ({@code rNNNN}), or empty
 * @param probe      a probe session ({@code X-Fast-Probe: 1}): house only
 * @param buffet     whether to add standalone ads for ad buffet ({@code X-Fast-Buffet: 0} turns them off)
 * @param evid       the break's SCTE-35 event id, or empty
 * @param upid       the break's segmentation UPID, or empty
 * @param durMs      the avail duration in milliseconds, or null when absent or not a number
 * @param userAgent  the viewer's user agent, as MediaTailor forwards it, or empty
 * @param xff        {@code X-Forwarded-For} as received, or empty
 * @param cfva       {@code CloudFront-Viewer-Address}, or empty
 * @param ctx        the content context header ({@code x-fast-ctx}), or empty
 * @param ctxLength  the content context header's length as received (0 when absent)
 */
public record Signals(String ticket, String sid, String pc, String fv, boolean probe, boolean buffet,
                      String evid, String upid, Integer durMs, String userAgent, String xff, String cfva,
                      String ctx, int ctxLength) {

    public static final int MAX_TICKET = 22;
    public static final int MAX_CONTEXT = 1000;
    static final int MAX_UPID = 255;
    static final int MAX_USER_AGENT = 2048;
    static final int MAX_XFF = 8192;
    static final int MAX_CFVA = 64;

    private static final Pattern NOT_TICKET = Pattern.compile("[^A-Za-z0-9_-]");
    private static final Pattern SID = Pattern.compile("[A-Za-z0-9-]{1,64}");
    private static final Pattern PC = Pattern.compile("[A-Za-z0-9_-]{1,64}");
    private static final Pattern FV = Pattern.compile("[A-Za-z0-9_-]{0,32}");
    private static final Pattern EVID = Pattern.compile("[0-9]{1,10}");
    private static final Pattern DIGITS = Pattern.compile("\\d{1,9}");

    public static Signals of(HttpRequestContext http) {
        final CaseInsensitiveMultiMap headers = http != null ? http.getHeaders() : CaseInsensitiveMultiMap.empty();
        final CaseInsensitiveMultiMap query = http != null ? http.getQueryParams() : CaseInsensitiveMultiMap.empty();
        final String ticket = NOT_TICKET.matcher(value(headers, "X-Fast-T")).replaceAll("");
        final String fv = value(headers, "X-Fast-Fv");
        final String ctx = value(headers, "x-fast-ctx");
        final String dur = value(query, "dur");
        return new Signals(
                ticket.substring(0, Math.min(ticket.length(), MAX_TICKET)),
                value(headers, "X-Fast-Sid"),
                value(headers, "X-Fast-Pc"),
                FV.matcher(fv).matches() ? fv : "",
                "1".equals(value(headers, "X-Fast-Probe")),
                !"0".equals(value(headers, "X-Fast-Buffet")),
                value(query, "evid"),
                atMost(value(query, "upid"), MAX_UPID),
                DIGITS.matcher(dur).matches() ? Integer.valueOf(dur) : null,
                atMost(value(headers, "User-Agent"), MAX_USER_AGENT),
                atMost(value(headers, "X-Forwarded-For"), MAX_XFF),
                atMost(value(headers, "CloudFront-Viewer-Address"), MAX_CFVA),
                atMost(ctx, MAX_CONTEXT),
                ctx.length());
    }

    /**
     * The first identifying value that is missing or malformed ({@code sid}, {@code pc}, {@code evid},
     * {@code dur_ms}), or null when the break can be asked about.
     */
    public String problem() {
        if (!SID.matcher(sid).matches()) {
            return "sid";
        }
        if (!PC.matcher(pc).matches()) {
            return "pc";
        }
        if (!EVID.matcher(evid).matches()) {
            return "evid";
        }
        return durMs == null ? "dur_ms" : null;
    }

    /**
     * The id of this break's decision: {@code <sid>.<evid>}.
     */
    public String decisionId() {
        return sid + "." + evid;
    }

    private static String value(CaseInsensitiveMultiMap map, String name) {
        return Objects.requireNonNullElse(map.get(name), "");
    }

    private static String atMost(String value, int length) {
        return value.length() <= length ? value : "";
    }
}
