import { database, todos } from "__SCOPE__/db";

export interface Todo {
  readonly id: string;
  readonly title: string;
}

export async function addTodo(title: string): Promise<void> {
  const trimmed = title.trim();
  if (!trimmed) return;
  const db = await database();
  await db.insert(todos).values({ title: trimmed });
}

export async function listTodos(): Promise<readonly Todo[]> {
  const db = await database();
  return db
    .select({ id: todos.id, title: todos.title })
    .from(todos)
    .orderBy(todos.createdAt);
}
