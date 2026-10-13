"""Memory-owned collections and authorized context queries; no Runtime imports."""
from kong.lexical import lexical_terms
from kong.memory.domain import AccessState, Bottle, BottlePolicy, Capability, Identity, IdentityVersion, MemoryContext, RetrievalQuery, Scope
from kong.memory.errors import NotFound, PolicyViolation
from kong.memory.policy import require, require_scope


class BottleOperations:
    @staticmethod
    def _bottle(db, auth, bottle_id):
        row = db.execute("SELECT * FROM bottles WHERE profile_id=? AND bottle_id=?", (auth.profile_id, bottle_id)).fetchone()
        if row is None:
            raise NotFound("Bottle is unavailable")
        require_scope(auth, Scope(kind=row["scope_kind"], id=row["scope_id"]))
        return row

    @staticmethod
    def _bottle_dto(row):
        return Bottle(bottle_id=row["bottle_id"], profile_id=row["profile_id"], title=row["title"], kind=row["kind"],
                      policy=BottlePolicy(scope=Scope(kind=row["scope_kind"], id=row["scope_id"])),
                      active=bool(row["active"]), revision=row["revision"])

    def create_bottle(self, auth, bottle: Bottle, *, command_id):
        require_scope(auth, bottle.policy.scope)
        if bottle.profile_id != auth.profile_id or bottle.active or bottle.revision != 1:
            raise PolicyViolation("New Bottles must belong to this Profile and start inactive")
        if not bottle.bottle_id.strip() or len(bottle.bottle_id) > 200:
            raise ValueError("Invalid Bottle ID")
        def action(db):
            if db.execute("SELECT 1 FROM bottles WHERE profile_id=? AND bottle_id=?", (auth.profile_id, bottle.bottle_id)).fetchone():
                raise PolicyViolation("Bottle already exists")
            db.execute("INSERT INTO bottles VALUES(?,?,?,?,?,?,0,1)",
                       (auth.profile_id, bottle.bottle_id, bottle.title, bottle.kind, bottle.policy.scope.kind, bottle.policy.scope.id))
            return {"bottle_id": bottle.bottle_id, "revision": 1}
        return self._execute(auth, command_id, "create_bottle", bottle.model_dump(mode="json"), Capability.BOTTLE, action)

    def list_bottles(self, auth):
        require(auth, Capability.READ)
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            return tuple(self._bottle_dto(row) for row in db.execute("SELECT * FROM bottles WHERE profile_id=? ORDER BY bottle_id", (auth.profile_id,))
                         if Scope(kind=row["scope_kind"], id=row["scope_id"]) in auth.scopes)

    def _membership(self, auth, bottle_id, record_id, command_id, *, add):
        def action(db):
            bottle = self._bottle(db, auth, bottle_id)
            record = self._row(db, auth, record_id)
            if bottle["kind"] != "memory":
                raise PolicyViolation("Persona/Relationship Bottles are reserved metadata, not Memory collections")
            if (bottle["scope_kind"], bottle["scope_id"]) != (record["scope_kind"], record["scope_id"]):
                raise PolicyViolation("Bottle and record scopes must match")
            if add:
                db.execute("INSERT OR IGNORE INTO record_collections VALUES(?,?,?)", (auth.profile_id, record_id, bottle_id))
            else:
                db.execute("DELETE FROM record_collections WHERE profile_id=? AND record_id=? AND collection_id=?",
                           (auth.profile_id, record_id, bottle_id))
            db.execute("UPDATE bottles SET revision=revision+1 WHERE profile_id=? AND bottle_id=?", (auth.profile_id, bottle_id))
            db.execute("UPDATE profiles SET activation_revision=activation_revision+1 WHERE profile_id=?", (auth.profile_id,))
            return {"record_id": record_id, "bottle_id": bottle_id, "revision": bottle["revision"] + 1}
        return self._execute(auth, command_id, "bottle_add" if add else "bottle_remove",
                             {"bottle_id": bottle_id, "record_id": record_id}, Capability.BOTTLE, action)

    def add_memory_to_bottle(self, auth, bottle_id, record_id, *, command_id):
        return self._membership(auth, bottle_id, record_id, command_id, add=True)

    def remove_memory_from_bottle(self, auth, bottle_id, record_id, *, command_id):
        return self._membership(auth, bottle_id, record_id, command_id, add=False)

    def _activation(self, auth, bottle_id, expected_revision, command_id, *, active):
        def action(db):
            bottle = self._bottle(db, auth, bottle_id)
            self._revision(bottle, expected_revision)
            if bottle["kind"] != "memory":
                raise PolicyViolation("Only Memory Bottles can be activated in this MVP")
            db.execute("UPDATE bottles SET active=?,revision=revision+1 WHERE profile_id=? AND bottle_id=?",
                       (int(active), auth.profile_id, bottle_id))
            db.execute("UPDATE profiles SET activation_revision=activation_revision+1 WHERE profile_id=?", (auth.profile_id,))
            return {"bottle_id": bottle_id, "revision": expected_revision + 1}
        return self._execute(auth, command_id, "activate_bottle" if active else "deactivate_bottle",
                             {"bottle_id": bottle_id, "expected_revision": expected_revision}, Capability.BOTTLE, action)

    def activate_bottle(self, auth, bottle_id, *, expected_revision, command_id):
        return self._activation(auth, bottle_id, expected_revision, command_id, active=True)

    def deactivate_bottle(self, auth, bottle_id, *, expected_revision, command_id):
        return self._activation(auth, bottle_id, expected_revision, command_id, active=False)

    def list_bottle_memories(self, auth, bottle_id, *, limit=100):
        require(auth, Capability.READ)
        if auth.actor != "owner" or not 1 <= limit <= 1000:
            raise PolicyViolation("Collection inspection requires the owner and a bounded limit")
        with self._store.transaction(write=False) as db:
            self._profile(db, auth)
            self._bottle(db, auth, bottle_id)
            rows = db.execute("""SELECT r.* FROM memory_records r JOIN record_collections c
                ON c.profile_id=r.profile_id AND c.record_id=r.record_id
                WHERE r.profile_id=? AND c.collection_id=? AND r.state!='deleted' ORDER BY r.record_id LIMIT ?""",
                              (auth.profile_id, bottle_id, limit)).fetchall()
            return tuple(self._record(db, auth, self._row(db, auth, row["record_id"])) for row in rows)

    def _access(self, db, auth):
        profile = self._profile(db, auth)
        return AccessState(store_id=db.execute("SELECT value FROM metadata WHERE key='store_id'").fetchone()[0],
                           owner_id=auth.owner_id, profile_id=auth.profile_id, scopes=auth.scopes,
                           memory_revision=profile["memory_revision"], deletion_epoch=profile["deletion_epoch"],
                           activation_revision=profile["activation_revision"])

    def get_access_state(self, auth):
        require(auth, Capability.READ)
        with self._store.transaction(write=False) as db:
            return self._access(db, auth)

    def retrieve_context(self, auth, query, *, limit=12):
        """One consistent read snapshot. Never creates candidates or writes access logs."""
        require(auth, Capability.READ)
        if not isinstance(query, (str, RetrievalQuery)) or isinstance(query, str) and len(query) > 2000 or not 1 <= limit <= 100:
            raise ValueError("Context query exceeds its bounded limits")
        with self._store.transaction(write=False) as db:
            access = self._access(db, auth)
            profile = self._profile(db, auth)
            identity_row = db.execute("SELECT * FROM identity_versions WHERE profile_id=? AND version=?",
                                      (auth.profile_id, profile["identity_version"])).fetchone()
            identity = None if identity_row is None else IdentityVersion(profile_id=auth.profile_id,
                version=identity_row["version"], identity=Identity.model_validate_json(identity_row["content"]),
                approved_by=identity_row["approved_by"], created_at=identity_row["created_at"])
            # A derived child is readable only if every transitive input is still
            # readable. Giving its own Bottle membership never widens its inputs.
            prefix = """WITH RECURSIVE visible(id) AS (
                SELECT r.record_id FROM memory_records r WHERE r.profile_id=? AND r.owner_id=? AND r.state='active'
                AND (r.expires_at IS NULL OR julianday(r.expires_at)>julianday(?)) AND (
            """
            params = [auth.profile_id, auth.owner_id, self.clock()]
            prefix += " OR ".join("(r.scope_kind=? AND r.scope_id=?)" for _ in auth.scopes) or "0"
            for scope in auth.scopes:
                params.extend((scope.kind, scope.id))
            prefix += """ ) AND EXISTS(SELECT 1 FROM record_collections c JOIN bottles b
                ON b.profile_id=c.profile_id AND b.bottle_id=c.collection_id
                WHERE c.profile_id=r.profile_id AND c.record_id=r.record_id AND b.active=1 AND b.kind='memory')),
                ancestors(child,parent,version) AS (
                    SELECT child_id,parent_id,parent_version FROM derivations WHERE profile_id=?
                    UNION SELECT a.child,d.parent_id,d.parent_version FROM ancestors a JOIN derivations d
                    ON d.child_id=a.parent WHERE d.profile_id=?),
                permitted(id) AS (SELECT id FROM visible WHERE NOT EXISTS(
                    SELECT 1 FROM ancestors a JOIN memory_records p ON p.profile_id=? AND p.record_id=a.parent
                    WHERE a.child=visible.id AND (a.parent NOT IN (SELECT id FROM visible) OR p.head_version!=a.version)))
            """
            params.extend((auth.profile_id, auth.profile_id, auth.profile_id))
            # The behavioral eval exposed English function-word contamination.
            # Filter only query terms; stored evidence and the Runtime remain intact.
            stopwords = {"what", "is", "are", "the", "a", "an", "our", "we", "i", "you", "it", "of", "to", "and", "how", "was", "did", "please"}
            strong = ()
            if isinstance(query, RetrievalQuery):
                from kong.memory.retrieval import search_terms
                strong, terms = search_terms(query)
                query_text = query.text
            else:
                terms = [term for term in lexical_terms(query) if term not in stopwords][:64]
                query_text = query
            if isinstance(query, RetrievalQuery) and query.episode_ids:
                placeholders = ",".join("?" for _ in query.episode_ids)
                rows = db.execute(prefix + f"SELECT r.* FROM memory_records r WHERE r.profile_id=? AND r.record_id IN (SELECT id FROM permitted) AND r.record_id IN ({placeholders}) ORDER BY r.record_id",
                                  (*params, auth.profile_id, *query.episode_ids)).fetchall()
            elif query_text.strip() and not terms:
                rows = []
            elif terms:
                expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
                if strong:
                    entity_expression = " OR ".join("(" + " AND ".join('"' + t.replace('"', '""') + '"' for t in lexical_terms(entity)) + ")" for entity in strong)
                    expression = "(" + entity_expression + ") AND (" + expression + ")"
                rows = db.execute(prefix + """SELECT r.* FROM memory_fts JOIN memory_records r
                    ON r.profile_id=memory_fts.profile_id AND r.record_id=memory_fts.record_id
                    WHERE memory_fts MATCH ? AND r.profile_id=? AND r.record_id IN (SELECT id FROM permitted)
                    ORDER BY bm25(memory_fts),r.record_id LIMIT ?""", (*params, expression, auth.profile_id, min(100, limit * 4) if isinstance(query, RetrievalQuery) else limit)).fetchall()
            else:
                rows = db.execute(prefix + "SELECT r.* FROM memory_records r WHERE r.profile_id=? AND r.record_id IN (SELECT id FROM permitted) ORDER BY r.record_id LIMIT ?",
                                  (*params, auth.profile_id, limit)).fetchall()
            relationship = self._relationship(db, auth) if Scope() in auth.scopes else None
            records = [self._record(db, auth, row) for row in rows]
            status, reason = ("available" if records else "no_usable_information"), "lexical"
            if isinstance(query, RetrievalQuery):
                from kong.memory.retrieval import constrain
                records, status, reason = constrain(records, query, strong)
            # Cherishing is collection-specific. Only a still-active membership
            # can influence records already selected through the hard filters.
            placeholders = ",".join("?" for _ in records) or "NULL"
            pins = {row[0] for row in db.execute(f"""SELECT p.record_id FROM memory_pins p
                JOIN bottles b ON b.profile_id=p.profile_id AND b.bottle_id=p.bottle_id
                JOIN record_collections c ON c.profile_id=p.profile_id AND c.record_id=p.record_id
                    AND c.collection_id=p.bottle_id
                WHERE p.profile_id=? AND b.active=1 AND b.kind='memory'
                    AND p.record_id IN ({placeholders})""", (auth.profile_id, *(r.record_id for r in records)))}
            records.sort(key=lambda record: record.record_id not in pins)
            records = records[:limit]
            return MemoryContext(access=access, identity=identity, relationship=relationship,
                records=tuple(records), query_status=status, query_reason=reason,
                pinned_record_ids=tuple(record.record_id for record in records if record.record_id in pins))
