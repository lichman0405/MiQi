import type { TransportStatus } from "../lib/ipc";
import "./ConnectionStatus.css";

interface ConnectionStatusProps {
  status: TransportStatus;
}

const STATUS_CONFIG: Record<TransportStatus, { label: string; className: string }> = {
  connected: { label: "Connected", className: "conn-connected" },
  connecting: { label: "Connecting…", className: "conn-connecting" },
  disconnected: { label: "Disconnected", className: "conn-disconnected" },
  restarting: { label: "Reconnecting…", className: "conn-connecting" },
  mock: { label: "Mock mode", className: "conn-mock" },
};

export function ConnectionStatus({ status }: ConnectionStatusProps) {
  const config = STATUS_CONFIG[status];

  return (
    <div className={`connection-status ${config.className}`}>
      <span className="conn-dot" />
      <span className="conn-label">{config.label}</span>
    </div>
  );
}
