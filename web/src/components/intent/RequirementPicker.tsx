/** Which requirements a scenario proves: picked by title, never typed by id. */
export default function RequirementPicker({
  label,
  options,
  selected,
  onChange,
}: {
  label: string
  options: Array<{ id: string; title: string }>
  selected: string[]
  onChange: (ids: string[]) => void
}) {
  return (
    <fieldset className="plane-picker">
      <legend>{label}</legend>
      {options.length === 0 && <span className="muted">Add a requirement first.</span>}
      {options.map((option) => {
        const on = selected.includes(option.id)
        return (
          <label className={on ? 'on' : ''} key={option.id}>
            <input
              type="checkbox"
              checked={on}
              onChange={(event) =>
                onChange(
                  event.target.checked
                    ? [...selected, option.id]
                    : selected.filter((id) => id !== option.id),
                )
              }
            />
            {option.title.trim() || 'Untitled requirement'}
          </label>
        )
      })}
    </fieldset>
  )
}
