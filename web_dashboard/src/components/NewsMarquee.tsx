"use client";

import React from "react";
import { Radio, ExternalLink, AlertTriangle } from "lucide-react";
import { SentinelHeadline } from "@/types/telemetry";

interface NewsMarqueeProps {
  headlines?: SentinelHeadline[];
}

export const NewsMarquee: React.FC<NewsMarqueeProps> = ({ headlines = [] }) => {
  const displayHeadlines =
    headlines && headlines.length > 0
      ? headlines
      : [
          {
            title: "NSE Market Ticker: Live telemetry & risk gatekeepers active",
            summary: "",
            source: "NSE Live",
            time: "09:00 IST",
            category: "DOMESTIC_CORPORATE",
            url: "https://www.nseindia.com",
          },
        ];

  const renderHeadlineItem = (item: SentinelHeadline, key: string) => {
    const isNotice =
      item.category === "MARKET_NOTICE" ||
      /NSE MARKET NOTICE|MARKETS CLOSED/i.test(item.title);
    const isSevere =
      !isNotice &&
      /sebi|raid|fraud|default|ban|merger|earnings|results|rbi|downgrade|crash|plunge/i.test(
        item.title
      );

    return (
      <div key={key} className="inline-flex items-center space-x-2 shrink-0">
        {/* Source Badge */}
        <span
          className={`text-[10px] px-1.5 py-0.5 rounded font-semibold tracking-tight inline-flex items-center space-x-1 ${
            isNotice
              ? "bg-amber-500/20 text-amber-300 border border-amber-500/40"
              : isSevere
              ? "bg-rose-500/20 text-rose-400 border border-rose-500/40"
              : "bg-slate-800 text-slate-400 border border-slate-700/60"
          }`}
        >
          {isNotice && <AlertTriangle className="h-2.5 w-2.5 text-amber-400 mr-0.5" />}
          <span>{item.source}</span>
          <span className="opacity-50">•</span>
          <span>{item.time}</span>
        </span>

        {/* Headline Link / Text */}
        {item.url ? (
          <a
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            className={`group inline-flex items-center space-x-1.5 transition underline-offset-2 hover:underline ${
              isNotice
                ? "text-amber-200 font-semibold hover:text-amber-100 decoration-amber-400"
                : isSevere
                ? "text-rose-200 hover:text-white decoration-rose-400"
                : "text-slate-300 hover:text-white decoration-cyan-400/70"
            }`}
          >
            <span>{item.title}</span>
            <ExternalLink className="h-3 w-3 text-slate-500 group-hover:text-cyan-400 opacity-60 group-hover:opacity-100 transition-opacity shrink-0 inline-block ml-0.5" />
          </a>
        ) : (
          <span
            className={`transition cursor-default ${
              isNotice
                ? "text-amber-200 font-semibold"
                : isSevere
                ? "text-rose-200"
                : "text-slate-300 hover:text-white"
            }`}
          >
            {item.title}
          </span>
        )}

        <span className="text-slate-600 select-none px-1">•</span>
      </div>
    );
  };

  return (
    <div className="w-full bg-cockpit-surface/90 border-b border-cockpit-border px-3 py-1.5 flex items-center space-x-3 overflow-hidden text-xs font-mono select-none">
      {/* Marquee Badge */}
      <div className="flex items-center space-x-1.5 flex-shrink-0 px-2 py-0.5 rounded bg-cockpit-blue/15 border border-cockpit-blue/30 text-cockpit-blue font-bold text-[11px]">
        <Radio className="h-3 w-3 animate-pulse text-cyan-400" />
        <span className="tracking-wider">SENTINEL NEWSWIRE</span>
      </div>

      {/* Scrolling Track: 2 Identical side-by-side flex tracks for seamless infinite loop */}
      <div className="flex-1 overflow-hidden relative">
        <div className="flex w-max shrink-0 animate-marquee hover:[animation-play-state:paused]">
          {/* Primary Track */}
          <div className="flex shrink-0 items-center space-x-6 pr-6">
            {displayHeadlines.map((item, idx) =>
              renderHeadlineItem(item, `track1-${idx}`)
            )}
          </div>
          {/* Secondary Track (Seamless Loop) */}
          <div
            className="flex shrink-0 items-center space-x-6 pr-6"
            aria-hidden="true"
          >
            {displayHeadlines.map((item, idx) =>
              renderHeadlineItem(item, `track2-${idx}`)
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
