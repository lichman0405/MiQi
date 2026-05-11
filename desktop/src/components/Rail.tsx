import type { RailTab } from "../App";
import "./Rail.css";

interface RailProps {
  activeTab: RailTab;
  onTabChange: (tab: RailTab) => void;
  onToggleTheme: () => void;
  theme: "light" | "dark";
}

const TABS: { id: RailTab; icon: string; label: string }[] = [
  { id: "chats", icon: "💬", label: "Chats" },
  { id: "files", icon: "📁", label: "Files" },
  { id: "tools", icon: "🔧", label: "Tools" },
  { id: "memory", icon: "🧠", label: "Memory" },
  { id: "cron", icon: "⏱", label: "Cron" },
  { id: "settings", icon: "⚙", label: "Settings" },
];

export function Rail({ activeTab, onTabChange, onToggleTheme, theme }: RailProps) {
  return (
    <nav className="rail" aria-label="Main navigation">
      <div className="rail-tabs">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            className={`rail-tab ${activeTab === tab.id ? "rail-tab--active" : ""}`}
            onClick={() => onTabChange(tab.id)}
            title={tab.label}
            aria-label={tab.label}
            aria-pressed={activeTab === tab.id}
          >
            <span className="rail-tab-icon">{tab.icon}</span>
          </button>
        ))}
      </div>
      <div className="rail-bottom">
        <button className="rail-tab" onClick={onToggleTheme} title={`Switch to ${theme === "light" ? "dark" : "light"} theme`} aria-label="Toggle theme">
          <span className="rail-tab-icon">{theme === "light" ? "🌙" : "☀"}</span>
        </button>
      </div>
    </nav>
  );
}
