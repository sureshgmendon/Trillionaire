"use client";

import React from "react";
import { Radio, ExternalLink } from "lucide-react";
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
    return (
      <div key={key} className="inline-flex items-center space-x-2 shrink-0">
        {/* Source Badge: Subtle dark terminal pill */}
        <span className="text-[10px] px-1.5 py-0.5 rounded font-medium tracking-tight inline-flex items-center space-x-1 bg-slate-800/80 text-slate-400 border border-slate-700/60">
          <span>{item.source}</span>
          <span className="opacity-40">•</span>
          <span>{item.time}</span>
        </span>

        {/* Headline Link / Text: Crisp text-slate-300 with cyan-300 hover */}
        {item.url ? (
          <a
            href={item.url}
            target="_blank"
            rel="noopener noreferrer"
            className="group inline-flex items-center space-x-1.5 transition text-slate-300 hover:text-cyan-300 underline-offset-2 hover:underline decoration-cyan-400/50"
          >
            <span>{item.title}</span>
            <ExternalLink className="h-3 w-3 text-slate-500 group-hover:text-cyan-400 opacity-60 group-hover:opacity-100 transition-opacity shrink-0 inline-block ml-0.5" />
          </a>
        ) : (
          <span className="text-slate-300 hover:text-cyan-300 transition cursor-default">
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
