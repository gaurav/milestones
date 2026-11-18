// The search box every page has: a search field with a × at its end, there only while the field
// has text, which empties it and leaves the cursor in it.
//
// The browser's own clear button is hidden (base.css): Firefox draws none, Safari drops it once
// the field has a border of its own, and where Chrome does draw one it would sit beside this.
import { useRef } from 'preact/hooks';

export function SearchBox({ value, onInput, placeholder, id, class: cls, onKeyDown }: {
  value: string;
  onInput: (value: string) => void;
  placeholder: string;
  id?: string;
  class?: string;
  onKeyDown?: (e: KeyboardEvent) => void;
}) {
  const field = useRef<HTMLInputElement>(null);
  return (
    <span class="search">
      <input ref={field} id={id} type="search" placeholder={placeholder} value={value} class={cls}
        onInput={(e) => onInput((e.target as HTMLInputElement).value)} onKeyDown={onKeyDown} />
      {value && (
        <button type="button" class="search-clear" title="Clear the search" aria-label="Clear the search"
          onClick={() => { onInput(''); field.current?.focus(); }}>×</button>
      )}
    </span>
  );
}
