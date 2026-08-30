import { revalidatePath } from "next/cache";

import { addTodo, listTodos } from "__SCOPE__/domain";

// Read on every request: the list is state, not content. A page that read the
// database while `next build` prerendered it would fail, and should.
export const dynamic = "force-dynamic";

const route = "__ROUTE__";

async function add(formData: FormData): Promise<void> {
  "use server";
  await addTodo(String(formData.get("title") ?? ""));
  revalidatePath(route);
}

export default async function CapabilityPage() {
  const items = await listTodos();
  return (
    <main className="shell">
      <p className="eyebrow">Persistence spike</p>
      <h1>Todo</h1>
      <form action={add}>
        <label htmlFor="title">New item</label>
        <input id="title" name="title" type="text" autoComplete="off" />
        <button type="submit">Add</button>
      </form>
      <ul aria-label="Items">
        {items.map((item) => (
          <li key={item.id}>{item.title}</li>
        ))}
      </ul>
    </main>
  );
}
