# Depois da viagem

Escopo congelado em 24/08/2026, sete semanas antes da viagem: o que o bot faz
até outubro é `/status`, `/menores` com contexto, `/perto`, `/confianca` e os
alertas. O ativo que importa agora é a série histórica — cada dia de coleta
que falha é um dia que não volta; comando novo dá para escrever no avião.

Ideias avaliadas e adiadas de propósito, com o motivo, para ninguém rediscutir
do zero:

- **`/rendimento` (ranking custo-benefício)** — `duração / (fila + duração)`.
  Analisado em 24/08: a fórmula favorece atração longa quase sempre
  (Kilimanjaro venceria com uma hora de fila) e é cega para barganha — Seven
  Dwarfs a 30 min de fila renderia mal, sendo que a fila típica dele é 85.
  Barganha é fila contra fila histórica, que é o que o `/confianca` já mede.
  Se um dia entrar, é comando separado e a regra 12 ganha exceção escrita;
  nunca no `/menores`, no `/perto` ou nos alertas.
- **Nota de avaliação no `/menores`** — o TouringPlans publica (4.5/5 e
  contagem), mas não existe em nenhum arquivo do projeto. Entrar exige
  expandir o coletor e criar mais um dado curado para manter.
- **Fórmula composta de recomendação** (fila + caminhada + duração + nota) —
  qualquer soma dessas grandezas esbarra na regra 12, e o valor real só dá
  para julgar depois de usar o sistema em campo.
- **Horário oficial via themeparks.wiki** — hoje o horário sai do histórico
  (`horario_operacao`), o que erra em dia atípico. O calendário oficial é
  público e resolveria; avaliar depois de ver quanto o medido erra na prática.

O teste que decide tudo isso é a viagem: abrir o app na fila, com calor e
pressa, e ver o que a tela responde em três segundos — e o que faltou de
verdade.
