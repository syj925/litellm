import styles from "./GatewayLanding.module.css";

export default function OneWordmark() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 1530 480"
      width="1530"
      height="480"
      fill="none"
      role="img"
      aria-labelledby="gateway-one-wordmark-title gateway-one-wordmark-description"
    >
      <title id="gateway-one-wordmark-title">ONE</title>
      <desc id="gateway-one-wordmark-description">
        Three interlocking graphite letterforms joined by one electric-yellow connection, with fine terminal echoes.
      </desc>

      <defs>
        <clipPath id="gateway-one-main-lane" clipPathUnits="userSpaceOnUse">
          <rect x="148" y="561" width="1219" height="16" rx="8" />
        </clipPath>
      </defs>
      <g transform="translate(-25 -334)" shapeRendering="geometricPrecision">
        <g className={styles["one-echoes"]} aria-hidden="true">
          <rect x="1107" y="352" width="336" height="84" rx="42" />
          <rect x="1107" y="352" width="387" height="84" rx="42" />
          <rect x="1107" y="352" width="441" height="84" rx="42" />
          <rect x="1107" y="519" width="326" height="84" rx="42" />
          <rect x="1107" y="519" width="382" height="84" rx="42" />
          <rect x="1107" y="519" width="439" height="84" rx="42" />
          <rect x="1107" y="705" width="341" height="87" rx="43.5" />
          <rect x="1107" y="705" width="394" height="87" rx="43.5" />
          <rect x="1107" y="705" width="447" height="87" rx="43.5" />
        </g>
        <g className={styles["one-glyphs"]} fillRule="evenodd" clipRule="evenodd">
          <path
            className={[styles["one-glyph"], styles["one-glyph-o"]].join(" ")}
            d="M279 343C431 343 521 431 521 573C521 714 431 803 279 803C125 803 33 715 33 573C33 431 125 343 279 343ZM279 429C195 429 144 485 144 573C144 660 195 715 279 715C363 715 416 660 416 573C416 485 363 429 279 429Z"
          />

          <path
            className={[styles["one-glyph"], styles["one-glyph-n"]].join(" ")}
            d="M543 352H651C657 352 661 355 665 360L880 641V352H985V793H873C868 793 864 790 860 785L642 486V793H543V352Z"
          />

          <path
            className={[styles["one-glyph"], styles["one-glyph-e"]].join(" ")}
            d="M1110 352H1364C1421 352 1421 436 1364 436H1139C1124 436 1117 444 1117 459V519H1348C1404 519 1404 603 1348 603H1117V681C1117 697 1124 705 1140 705H1364C1422 705 1422 792 1364 792H1110C1050 792 1014 755 1014 695V449C1014 391 1052 352 1110 352Z"
          />

          <path className={styles["one-backbone"]} d="M136 546H1348V594H136V546Z" />
        </g>
        <g className={styles["one-traces"]} aria-hidden="true">
          <path
            className={[styles["one-trace"], styles["one-trace-main"]].join(" ")}
            pathLength="1"
            d="M148 569H1367"
          />
          <path
            className={[styles["one-trace"], styles["one-trace-terminal"], styles["one-trace-top"]].join(" ")}
            pathLength="1"
            d="M1066 569V454Q1066 396 1124 396H1380"
          />
          <path
            className={[styles["one-trace"], styles["one-trace-terminal"], styles["one-trace-bottom"]].join(" ")}
            pathLength="1"
            d="M1066 569V690Q1066 749 1125 749H1380"
          />
        </g>
        <g className={styles["one-energy"]} aria-hidden="true" fill="var(--one-signal, #e1f20a)">
          <g clipPath="url(#gateway-one-main-lane)">
            <g className={[styles["one-packet"], styles["one-packet-main"]].join(" ")}>
              <rect x="60" y="564" width="150" height="10" rx="5" />
              <rect x="181" y="564" width="29" height="10" rx="5" fill="#f7ffd6" />
            </g>
          </g>
          <g className={[styles["one-packet"], styles["one-packet-branch"], styles["one-packet-top"]].join(" ")}>
            <circle r="7" />
            <circle r="3.5" fill="#f7ffd6" />
          </g>
          <g className={[styles["one-packet"], styles["one-packet-branch"], styles["one-packet-bottom"]].join(" ")}>
            <circle r="7" />
            <circle r="3.5" fill="#f7ffd6" />
          </g>
        </g>
      </g>
    </svg>
  );
}
