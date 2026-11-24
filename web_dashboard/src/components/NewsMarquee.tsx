"use client";

import React from "react";
import { Newspaper, Radio } from "lucide-react";
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
            title: "US Markets Close Mixed: S&P 500 flat, NASDAQ up 0.1% as bond yields steady",
            summary: "",
            source: "Global Markets",
            time: "08:30 IST",
            category: "GLOBAL_MACRO",
          },
          {
            title: "Asian Markets Open Steady: Nikkei and Hang Seng trade with mild gains",
            summary: "",
            source: "Asia Wire",
            time: "08:35 IST",
            category: "GLOBAL_MACRO",
          },
          {
            title: "NSE Market Ticker: Zero-token intraday headline streaming active",
            summary: "",
            source: "NSE Live",
            time: "09:00 IST",
            category: "DOMESTIC_CORPORATE",
          },
        ];

  return (
    <div className="w-full bg-cockpit-surface/90 border-b border-cockpit-border px-3 py-1.5 flex items-center space-x-3 overflow-hidden text-xs font-mono select-none">
      {/* Marquee Badge */}
      <div className="flex items-center space-x-1.5 flex-shrink-0 px-2 py-0.5 rounded bg-cockpit-blue/15 border border-cockpit-blue/30 text-cockpit-blue font-bold text-[11px]">
        <Radio className="h-3 w-3 animate-pulse text-cyan-400" />
        <span className="tracking-wider">SENTINEL NEWSWIRE</span>
      </div>

      {/* Scrolling Track */}
      <div className="flex-1 overflow-hidden relative">
        <div className="flex space-x-8 animate-marquee whitespace-nowrap">
          {displayHeadlines.concat(displayHeadlines).map((item, idx) => {
            const isSevere = /sebi|raid|fraud|default|ban|merger|earnings|results|rbi|downgrade|crash/i.test(
              item.title
            );
            return (
              <div key={idx} className="inline-flex items-center space-x-2 text-slate-300">
                <span
                  className={`text-[10px] px-1.5 py-0.2 rounded font-semibold ${
                    isSevere
                      ? "bg-rose-500/20 text-rose-400 border border-rose-500/40"
                      : "bg-slate-800 text-slate-400"
                  }`}
                >
                  {item.source} • {item.time}
                </span>
                <span className="hover:text-white transition cursor-default">{item.title}</span>
                <span className="text-slate-600">•</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};
