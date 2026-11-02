/**
 * Siparişte personelin girdiği notlar (orders.admin_notes) — iade satırında AÇIKÇA gösterilir
 * (eskiden yalnız üzerine gelince görünen küçük bir rozetti, fark edilmiyordu).
 * En yeni not üstte; en fazla 3 not, fazlası "+N not daha" ile açılır.
 */
import { useState } from "react";

const fmt = (iso) => {
  try { return new Date(iso).toLocaleString("tr-TR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
};

export default function StaffNotes({ notes }) {
  const [all, setAll] = useState(false);
  if (!Array.isArray(notes) || notes.length === 0) return null;
  const list = [...notes].reverse();
  const shown = all ? list : list.slice(0, 3);
  return (
    <div className="mt-1.5 max-w-xs rounded border border-indigo-200 bg-indigo-50 px-2 py-1.5 text-left" data-testid="staff-notes">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-indigo-700">Sipariş Notları</div>
      <ul className="mt-1 space-y-1">
        {shown.map((n, i) => (
          <li key={i} className="text-[11px] leading-snug text-gray-800">
            <span className="whitespace-pre-wrap break-words">{n.text}</span>
            {(n.by || n.at) && (
              <span className="block text-[10px] text-gray-500">{[n.by, fmt(n.at)].filter(Boolean).join(" · ")}</span>
            )}
          </li>
        ))}
      </ul>
      {list.length > 3 && (
        <button type="button" onClick={(e) => { e.stopPropagation(); setAll((v) => !v); }}
          className="mt-1 text-[10px] font-medium text-indigo-700 hover:underline">
          {all ? "Daha az göster" : `+${list.length - 3} not daha`}
        </button>
      )}
    </div>
  );
}
