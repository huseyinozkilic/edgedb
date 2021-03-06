#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


from __future__ import annotations
from typing import *

import hashlib

from edb import errors

from edb.common import struct

from edb.edgeql import ast as qlast

from . import delta as sd
from . import inheriting
from . import objects as so
from . import schema as s_schema
from . import name as sn
from . import utils


ReferencedT = TypeVar('ReferencedT', bound='ReferencedObject')
ReferencedInheritingObjectT = TypeVar('ReferencedInheritingObjectT',
                                      bound='ReferencedInheritingObject')


class ReferencedObject(so.DerivableObject):

    #: True if the object has an explicit definition and is not
    #: purely inherited.
    is_owned = so.SchemaField(
        bool,
        default=False,
        inheritable=False,
        compcoef=0.909,
        reflection_method=so.ReflectionMethod.AS_LINK,
    )

    def get_subject(self, schema: s_schema.Schema) -> Optional[so.Object]:
        # NB: classes that inherit ReferencedObject define a `get_subject`
        # method dynamically, with `subject = SchemaField`
        raise NotImplementedError

    def get_referrer(self, schema: s_schema.Schema) -> Optional[so.Object]:
        return self.get_subject(schema)

    def delete(self, schema: s_schema.Schema) -> s_schema.Schema:
        context = sd.CommandContext(
            modaliases={},
            schema=schema,
            disable_dep_verification=True,
        )
        delta, cmd = self._get_command_stack(schema, context, sd.DeleteObject)

        with context(sd.DeltaRootContext(schema=schema, op=delta)):
            schema = delta.apply(schema, context)

        return schema

    def derive_ref(
        self: ReferencedT,
        schema: s_schema.Schema,
        referrer: so.QualifiedObject,
        *qualifiers: str,
        mark_derived: bool = False,
        attrs: Optional[Dict[str, Any]] = None,
        dctx: Optional[sd.CommandContext] = None,
        derived_name_base: Optional[str] = None,
        inheritance_merge: bool = True,
        preserve_path_id: Optional[bool] = None,
        inheritance_refdicts: Optional[AbstractSet[str]] = None,
        transient: bool = False,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> Tuple[s_schema.Schema, ReferencedT]:
        if name is None:
            derived_name: str = self.get_derived_name(
                schema, referrer, *qualifiers,
                mark_derived=mark_derived,
                derived_name_base=derived_name_base)
        else:
            derived_name = name

        if self.get_name(schema) == derived_name:
            raise errors.SchemaError(
                f'cannot derive {self!r}({derived_name}) from itself')

        derived_attrs: Dict[str, object] = {}

        if attrs is not None:
            derived_attrs.update(attrs)

        derived_attrs['name'] = derived_name
        derived_attrs['bases'] = so.ObjectList.create(schema, [self])

        mcls = type(self)
        referrer_class = type(referrer)

        refdict = referrer_class.get_refdict_for_class(mcls)
        reftype = referrer_class.get_field(refdict.attr).type
        refname = reftype.get_key_for_name(schema, derived_name)
        refcoll = referrer.get_field_value(schema, refdict.attr)
        existing = refcoll.get(schema, refname, default=None)

        cmdcls: Type[sd.ObjectCommand[so.Object]]
        cmdcls = sd.AlterObject if existing is not None else sd.CreateObject
        cmd = sd.get_object_delta_command(
            objtype=type(self),
            cmdtype=cmdcls,
            schema=schema,
            name=derived_name,
        )

        for k, v in derived_attrs.items():
            cmd.set_attribute_value(k, v)

        if existing is not None:
            new_bases = derived_attrs['bases']
            old_bases = existing.get_bases(schema)

            if new_bases != old_bases:
                assert isinstance(new_bases, so.ObjectList)
                removed_bases, added_bases = inheriting.delta_bases(
                    [b.get_name(schema) for b in old_bases.objects(schema)],
                    [b.get_name(schema) for b in new_bases.objects(schema)],
                )

                rebase_cmd = sd.get_object_delta_command(
                    objtype=type(self),
                    cmdtype=inheriting.RebaseInheritingObject,
                    schema=schema,
                    name=derived_name,
                    added_bases=added_bases,
                    removed_bases=removed_bases,
                )

                cmd.add(rebase_cmd)

        context = sd.CommandContext(modaliases={}, schema=schema)
        delta, parent_cmd = cmd._build_alter_cmd_stack(
            schema, context, self, referrer=referrer)

        with context(sd.DeltaRootContext(schema=schema, op=delta)):
            if not inheritance_merge:
                context.current().inheritance_merge = False

            if inheritance_refdicts is not None:
                context.current().inheritance_refdicts = inheritance_refdicts

            if mark_derived:
                context.current().mark_derived = True

            if transient:
                context.current().transient_derivation = True

            if preserve_path_id:
                context.current().preserve_path_id = True

            parent_cmd.add(cmd)
            schema = delta.apply(schema, context)

        derived: ReferencedT = schema.get(derived_name)

        return schema, derived

    def get_verbosename(
        self,
        schema: s_schema.Schema,
        *,
        with_parent: bool = False,
    ) -> str:
        vn = super().get_verbosename(schema)
        if with_parent:
            subject = self.get_subject(schema)
            if subject is not None:
                pn = subject.get_verbosename(schema, with_parent=True)
                return f'{vn} of {pn}'

        return vn


class ReferencedInheritingObject(
    so.DerivableInheritingObject,
    ReferencedObject,
):

    # Indicates that the object has been declared as
    # explicitly inherited.
    declared_overloaded = so.SchemaField(
        bool,
        default=False,
        compcoef=None,
        introspectable=False,
        inheritable=False,
        ephemeral=True,
    )

    def get_implicit_bases(
        self: ReferencedInheritingObjectT,
        schema: s_schema.Schema,
    ) -> List[ReferencedInheritingObjectT]:
        return [
            b for b in self.get_bases(schema).objects(schema)
            if not b.generic(schema)
        ]


class ReferencedObjectCommandMeta(sd.ObjectCommandMeta):
    _transparent_adapter_subclass: ClassVar[bool] = True
    _referrer_context_class: Optional[
        Type[sd.ObjectCommandContext[so.Object]]
    ] = None

    def __new__(mcls,
                name: str,
                bases: Tuple[type, ...],
                clsdct: Dict[str, Any],
                *,
                referrer_context_class: Optional[
                    Type[sd.ObjectCommandContext[so.Object]]
                ] = None,
                **kwargs: Any
                ) -> ReferencedObjectCommandMeta:
        cls = super().__new__(mcls, name, bases, clsdct, **kwargs)
        assert isinstance(cls, ReferencedObjectCommandMeta)
        if referrer_context_class is not None:
            cls._referrer_context_class = referrer_context_class
        return cls


class ReferencedObjectCommandBase(
    sd.QualifiedObjectCommand[ReferencedT],
    metaclass=ReferencedObjectCommandMeta,
):

    @classmethod
    def get_referrer_context_class(
        cls,
    ) -> Type[sd.ObjectCommandContext[so.Object]]:
        if cls._referrer_context_class is None:
            raise TypeError(
                f'referrer_context_class is not defined for {cls}')
        return cls._referrer_context_class

    @classmethod
    def get_referrer_context(
        cls,
        context: sd.CommandContext,
    ) -> Optional[sd.ObjectCommandContext[so.Object]]:
        """Get the context of the command for the referring object, if any.

        E.g. for a `create/alter/etc concrete link` command this would
        be the context of the `create/alter/etc type` command.
        """
        ctxcls = cls.get_referrer_context_class()
        ctx = context.get(ctxcls)  # type: ignore
        return cast(Optional[sd.ObjectCommandContext[so.Object]], ctx)

    @classmethod
    def get_referrer_context_or_die(
        cls,
        context: sd.CommandContext,
    ) -> sd.ObjectCommandContext[so.Object]:
        ctx = cls.get_referrer_context(context)
        if ctx is None:
            raise RuntimeError(f'no referrer context for {cls}')
        return ctx


class StronglyReferencedObjectCommand(
    ReferencedObjectCommandBase[ReferencedT]
):
    pass


class ReferencedObjectCommand(ReferencedObjectCommandBase[ReferencedT]):

    @classmethod
    def _classname_from_ast(cls,
                            schema: s_schema.Schema,
                            astnode: qlast.NamedDDL,
                            context: sd.CommandContext
                            ) -> sn.Name:
        name = super()._classname_from_ast(schema, astnode, context)

        parent_ctx = cls.get_referrer_context(context)
        if parent_ctx is not None:
            assert isinstance(parent_ctx.op, sd.QualifiedObjectCommand)
            referrer_name = parent_ctx.op.classname
            base_ref = utils.ast_to_object_shell(
                astnode.name,
                modaliases=context.modaliases,
                schema=schema,
                metaclass=cls.get_schema_metaclass(),
            )

            base_name = base_ref.name
            quals = cls._classname_quals_from_ast(
                schema, astnode, base_name, referrer_name, context)
            pnn = sn.get_specialized_name(base_name, referrer_name, *quals)
            name = sn.Name(name=pnn, module=referrer_name.module)

        assert isinstance(name, sn.Name)
        return name

    @classmethod
    def _classname_from_name(
        cls,
        name: sn.SchemaName,
        referrer_name: sn.SchemaName,
    ) -> sn.Name:
        base_name = sn.shortname_from_fullname(name)
        quals = cls._classname_quals_from_name(name)
        pnn = sn.get_specialized_name(base_name, referrer_name, *quals)
        return sn.Name(name=pnn, module=referrer_name.module)

    @classmethod
    def _classname_quals_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.NamedDDL,
        base_name: str,
        referrer_name: str,
        context: sd.CommandContext,
    ) -> Tuple[str, ...]:
        return ()

    @classmethod
    def _classname_quals_from_name(
        cls,
        name: sn.SchemaName,
    ) -> Tuple[str, ...]:
        return ()

    @classmethod
    def _name_qual_from_exprs(cls,
                              schema: s_schema.Schema,
                              exprs: Iterable[str]) -> str:
        m = hashlib.sha1()
        for expr in exprs:
            m.update(expr.encode())
        return m.hexdigest()

    def _get_ast_node(self,
                      schema: s_schema.Schema,
                      context: sd.CommandContext
                      ) -> Type[qlast.DDLOperation]:
        subject_ctx = self.get_referrer_context(context)
        ref_astnode: Type[qlast.DDLOperation] = getattr(self,
                                                        'referenced_astnode',
                                                        None)
        if subject_ctx is not None and ref_astnode is not None:
            return ref_astnode
        else:
            if isinstance(self.astnode, (list, tuple)):
                return self.astnode[1]
            else:
                return self.astnode

    def _build_alter_cmd_stack(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        scls: so.Object,
        *,
        referrer: Optional[so.Object] = None
    ) -> Tuple[sd.DeltaRoot, sd.Command]:

        delta = sd.DeltaRoot()

        if referrer is None:
            assert isinstance(scls, ReferencedObject)
            referrer = scls.get_referrer(schema)

        obj = referrer
        object_stack = []

        if type(self) != type(referrer):
            object_stack.append(referrer)

        while obj is not None:
            if isinstance(obj, ReferencedObject):
                obj = obj.get_referrer(schema)
                object_stack.append(obj)
            else:
                obj = None

        cmd: sd.Command = delta
        for obj in reversed(object_stack):
            assert obj is not None
            alter_cmd = obj.init_delta_command(schema, sd.AlterObject)
            cmd.add(alter_cmd)
            cmd = alter_cmd

        return delta, cmd


class CreateReferencedObject(
    ReferencedObjectCommand[ReferencedT],
    sd.CreateObject[ReferencedT],
):

    referenced_astnode: ClassVar[Type[qlast.ObjectDDL]]

    @classmethod
    def _cmd_tree_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: sd.CommandContext,
    ) -> sd.Command:
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)

        if isinstance(astnode, cls.referenced_astnode):
            objcls = cls.get_schema_metaclass()

            referrer_ctx = cls.get_referrer_context_or_die(context)
            referrer_class = referrer_ctx.op.get_schema_metaclass()
            referrer_name = referrer_ctx.op.classname
            refdict = referrer_class.get_refdict_for_class(objcls)

            cmd.set_attribute_value(
                refdict.backref_attr,
                so.ObjectShell(
                    name=referrer_name,
                    schemaclass=referrer_class,
                ),
            )

            cmd.set_attribute_value('is_owned', True)

            if getattr(astnode, 'is_abstract', None):
                cmd.set_attribute_value('is_abstract', True)

        return cmd

    def _get_ast_node(self,
                      schema: s_schema.Schema,
                      context: sd.CommandContext
                      ) -> Type[qlast.DDLOperation]:
        scls = self.get_object(schema, context)
        assert isinstance(scls, ReferencedInheritingObject)
        implicit_bases = scls.get_implicit_bases(schema)
        if implicit_bases and not context.declarative:
            mcls = self.get_schema_metaclass()
            Alter = sd.ObjectCommandMeta.get_command_class_or_die(
                sd.AlterObject, mcls)
            alter = Alter(classname=self.classname)
            return alter._get_ast_node(schema, context)
        else:
            return super()._get_ast_node(schema, context)

    @classmethod
    def as_inherited_ref_cmd(
        cls,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        astnode: qlast.ObjectDDL,
        parents: Any,
    ) -> sd.ObjectCommand[ReferencedT]:
        cmd = cls(classname=cls._classname_from_ast(schema, astnode, context))
        cmd.set_attribute_value('name', cmd.classname)
        return cmd

    @classmethod
    def as_inherited_ref_ast(cls,
                             schema: s_schema.Schema,
                             context: sd.CommandContext,
                             name: str,
                             parent: ReferencedObject) -> qlast.ObjectDDL:
        nref = cls.get_inherited_ref_name(schema, context, parent, name)
        astnode_cls = cls.referenced_astnode
        astnode = astnode_cls(name=nref)
        assert isinstance(astnode, qlast.ObjectDDL)
        return astnode

    @classmethod
    def get_inherited_ref_name(cls,
                               schema: s_schema.Schema,
                               context: sd.CommandContext,
                               parent: ReferencedObject,
                               name: str
                               ) -> qlast.ObjectRef:
        # reduce name to shortname
        if sn.Name.is_qualified(name):
            shortname: str = sn.shortname_from_fullname(sn.Name(name))
        else:
            shortname = name

        nref = qlast.ObjectRef(
            name=shortname,
            module=parent.get_shortname(schema).module,
        )

        return nref

    def _create_innards(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
    ) -> s_schema.Schema:
        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is None:
            return super()._create_innards(schema, context)
        else:
            referrer = referrer_ctx.scls
            schema = self._create_ref(schema, context, referrer)
            return super()._create_innards(schema, context)

    def _create_ref(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        referrer: so.Object,
    ) -> s_schema.Schema:
        referrer_cls = type(referrer)
        mcls = type(self.scls)
        refdict = referrer_cls.get_refdict_for_class(mcls)
        schema = referrer.add_classref(schema, refdict.attr, self.scls)
        return schema


class DeleteReferencedObjectCommand(
    ReferencedObjectCommand[ReferencedT],
    sd.DeleteObject[ReferencedT],
):

    def _delete_innards(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
    ) -> s_schema.Schema:
        schema = super()._delete_innards(schema, context)

        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is None:
            return schema
        else:
            referrer = referrer_ctx.scls
            schema = self._delete_ref(schema, context, referrer)
            return schema

    def _delete_ref(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        referrer: so.Object,
    ) -> s_schema.Schema:

        scls = self.scls
        referrer_class = type(referrer)
        mcls = type(scls)
        refdict = referrer_class.get_refdict_for_class(mcls)
        reftype = referrer_class.get_field(refdict.attr).type
        refname = reftype.get_key_for(schema, self.scls)

        return referrer.del_classref(schema, refdict.attr, refname)


class ReferencedInheritingObjectCommand(
    ReferencedObjectCommand[ReferencedInheritingObjectT],
    inheriting.InheritingObjectCommand[ReferencedInheritingObjectT],
):

    ref_op_propagated = struct.Field(bool, default=False)

    def _get_implicit_ref_bases(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        referrer: so.InheritingObject,
        referrer_field: str,
        fq_name: sn.SchemaName,
    ) -> List[ReferencedInheritingObjectT]:

        assert isinstance(referrer, so.QualifiedObject)
        child_referrer_bases = referrer.get_bases(schema).objects(schema)
        implicit_bases = []
        ref_field_type = type(referrer).get_field(referrer_field).type

        for ref_base in child_referrer_bases:
            fq_name_in_child = self._classname_from_name(
                fq_name, ref_base.get_name(schema))
            refname = ref_field_type.get_key_for_name(schema, fq_name_in_child)
            parent_coll = ref_base.get_field_value(schema, referrer_field)
            parent_item = parent_coll.get(schema, refname, default=None)
            if (parent_item is not None
                    and not parent_item.get_is_final(schema)):
                implicit_bases.append(parent_item)

        return implicit_bases

    def get_ref_implicit_base_delta(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        refcls: ReferencedInheritingObjectT,
        implicit_bases: List[ReferencedInheritingObjectT],
    ) -> inheriting.BaseDelta_T:
        child_bases = refcls.get_bases(schema).objects(schema)

        default_base = refcls.get_default_base_name()
        explicit_bases = [
            b for b in child_bases
            if b.generic(schema) and b.get_name(schema) != default_base
        ]

        new_bases = implicit_bases + explicit_bases
        return inheriting.delta_bases(
            [b.get_name(schema) for b in child_bases],
            [b.get_name(schema) for b in new_bases],
        )

    def _validate(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext
    ) -> None:
        scls = self.scls
        implicit_bases = [
            b for b in scls.get_bases(schema).objects(schema)
            if not b.generic(schema)
        ]

        referrer_ctx = self.get_referrer_context_or_die(context)
        objcls = self.get_schema_metaclass()
        referrer_class = referrer_ctx.op.get_schema_metaclass()
        refdict = referrer_class.get_refdict_for_class(objcls)

        if context.declarative and scls.get_is_owned(schema):
            if (implicit_bases
                    and refdict.requires_explicit_overloaded
                    and not self.get_attribute_value('declared_overloaded')):

                ancestry = []

                for obj in implicit_bases:
                    bref = obj.get_referrer(schema)
                    assert bref is not None
                    ancestry.append(bref)

                raise errors.SchemaDefinitionError(
                    f'{self.scls.get_verbosename(schema, with_parent=True)} '
                    f'must be declared using the `overloaded` keyword because '
                    f'it is defined in the following ancestor(s): '
                    f'{", ".join(a.get_shortname(schema) for a in ancestry)}',
                    context=self.source_context,
                )
            elif (not implicit_bases
                    and self.get_attribute_value('declared_overloaded')):

                raise errors.SchemaDefinitionError(
                    f'{self.scls.get_verbosename(schema, with_parent=True)}: '
                    f'cannot be declared `overloaded` as there are no '
                    f'ancestors defining it.',
                    context=self.source_context,
                )

    def get_implicit_bases(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        bases: Any,
    ) -> Sequence[str]:

        mcls = self.get_schema_metaclass()
        default_base = mcls.get_default_base_name()

        if isinstance(bases, so.ObjectCollectionShell):
            base_names = [b.get_name(schema) for b in bases.items]
        elif isinstance(bases, so.ObjectList):
            base_names = list(bases.names(schema))
        else:
            # assume regular iterable of shells
            base_names = [b.get_name(schema) for b in bases]

        # Filter out explicit bases
        implicit_bases = [
            b
            for b in base_names
            if (
                b != default_base
                and isinstance(b, sn.SchemaName)
                and sn.shortname_from_fullname(b) != b
            )
        ]

        return implicit_bases

    def _propagate_ref_op(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        scls: ReferencedInheritingObject,
        cb: Callable[[sd.ObjectCommand[so.Object], str], None]
    ) -> s_schema.Schema:
        for ctx in reversed(context.stack):
            if (
                isinstance(ctx.op, ReferencedInheritingObjectCommand)
                and ctx.op.ref_op_propagated
            ):
                return schema

        referrer_ctx = self.get_referrer_context_or_die(context)
        referrer = referrer_ctx.scls
        referrer_class = type(referrer)
        mcls = type(scls)
        refdict = referrer_class.get_refdict_for_class(mcls)
        reftype = referrer_class.get_field(refdict.attr).type
        refname = reftype.get_key_for(schema, self.scls)

        for descendant in scls.ordered_descendants(schema):
            d_alter_cmd = descendant.init_delta_command(schema, sd.AlterObject)
            assert isinstance(descendant, ReferencedObject)
            d_alter_cmd.ref_op_propagated = True
            d_referrer = descendant.get_referrer(schema)
            assert d_referrer is not None
            r_alter_cmd = d_referrer.init_delta_command(schema, sd.AlterObject)

            with r_alter_cmd.new_context(schema, context, d_referrer):
                with d_alter_cmd.new_context(schema, context, descendant):
                    cb(d_alter_cmd, refname)  # type: ignore

                r_alter_cmd.add(d_alter_cmd)

            self.add(r_alter_cmd)

        return schema

    def _drop_owned_refs(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        refdict: so.RefDict,
    ) -> s_schema.Schema:

        scls = self.scls
        refs = scls.get_field_value(schema, refdict.attr)

        for ref in refs.objects(schema):
            inherited = ref.get_implicit_bases(schema)
            if inherited and ref.get_is_owned(schema):
                drop_owned = ref.init_delta_command(schema, AlterOwned)
                drop_owned.set_attribute_value('is_owned', False)
                alter = ref.init_delta_command(schema, sd.AlterObject)
                alter.add(drop_owned)
                schema = alter.apply(schema, context)
                self.add(alter)
            else:
                drop_ref = ref.init_delta_command(schema, sd.DeleteObject)
                self.add(drop_ref)

        return schema


class CreateReferencedInheritingObject(
    CreateReferencedObject[ReferencedInheritingObjectT],
    inheriting.CreateInheritingObject[ReferencedInheritingObjectT],
    ReferencedInheritingObjectCommand[ReferencedInheritingObjectT],
):

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        refctx = type(self).get_referrer_context(context)
        if refctx is not None:
            if self.get_attribute_value('is_from_alias'):
                return None

            elif not self.get_attribute_value('is_owned'):
                if context.descriptive_mode:
                    astnode = super()._get_ast(
                        schema,
                        context,
                        parent_node=parent_node,
                    )
                    assert astnode is not None

                    inherited_from = [
                        sn.quals_from_fullname(b)[0]
                        for b in self.get_implicit_bases(
                            schema,
                            context,
                            self.get_attribute_value('bases'),
                        )
                    ]

                    astnode.system_comment = (
                        f'inherited from {", ".join(inherited_from)}'
                    )

                    return astnode
                else:
                    return None

            else:
                astnode = super()._get_ast(
                    schema, context, parent_node=parent_node)

                if context.declarative:
                    scls = self.get_object(schema, context)
                    assert isinstance(scls, ReferencedInheritingObject)
                    implicit_bases = scls.get_implicit_bases(schema)
                    objcls = self.get_schema_metaclass()
                    referrer_class = refctx.op.get_schema_metaclass()
                    refdict = referrer_class.get_refdict_for_class(objcls)
                    if refdict.requires_explicit_overloaded and implicit_bases:
                        assert astnode is not None
                        astnode.declared_overloaded = True

                return astnode
        else:
            return super()._get_ast(schema, context, parent_node=parent_node)

    def _create_begin(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
    ) -> s_schema.Schema:
        referrer_ctx = self.get_referrer_context(context)
        implicit_bases = None

        if referrer_ctx is not None and not context.canonical:
            objcls = self.get_schema_metaclass()
            referrer = referrer_ctx.scls

            if isinstance(referrer, so.InheritingObject):
                referrer_class = referrer_ctx.op.get_schema_metaclass()
                refdict = referrer_class.get_refdict_for_class(objcls)

                implicit_bases = self._get_implicit_ref_bases(
                    schema, context, referrer, refdict.attr, self.classname)

                if implicit_bases:
                    bases = self.get_attribute_value('bases')
                    if bases:
                        bases = so.ObjectList.create(
                            schema,
                            implicit_bases + [
                                b for b in bases.objects(schema)
                                if b not in implicit_bases
                            ],
                        )
                    else:
                        bases = so.ObjectList.create(
                            schema,
                            implicit_bases,
                        )

                    self.set_attribute_value('bases', bases)

        schema = super()._create_begin(schema, context)

        if referrer_ctx is not None and not context.canonical:
            self._validate(schema, context)

        return schema

    def _create_ref(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        referrer: so.Object,
    ) -> s_schema.Schema:

        schema = super()._create_ref(schema, context, referrer)

        if (not self.scls.get_is_final(schema)
                and isinstance(referrer, so.InheritingObject)
                and not context.canonical
                and context.enable_recursion):
            # Propagate the creation of a new ref to descendants of
            # our referrer.
            schema = self._propagate_ref_creation(schema, context, referrer)

        return schema

    def _propagate_ref_creation(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        referrer: so.InheritingObject,
    ) -> s_schema.Schema:

        get_cmd = sd.ObjectCommandMeta.get_command_class_or_die

        mcls = type(self.scls)
        referrer_cls = type(referrer)
        alter_cmd = get_cmd(sd.AlterObject, referrer_cls)
        ref_create_cmd = get_cmd(sd.CreateObject, mcls)
        ref_alter_cmd = get_cmd(sd.AlterObject, mcls)
        ref_rebase_cmd = get_cmd(inheriting.RebaseInheritingObject, mcls)
        assert issubclass(ref_create_cmd, CreateReferencedInheritingObject)
        assert issubclass(ref_rebase_cmd, RebaseReferencedInheritingObject)
        refdict = referrer_cls.get_refdict_for_class(mcls)
        parent_fq_refname = self.scls.get_name(schema)

        for child in referrer.children(schema):
            if not child.allow_ref_propagation(schema, context, refdict):
                continue

            alter = alter_cmd(classname=child.get_name(schema))
            with alter.new_context(schema, context, child):
                # This is needed to get the correct inherited name which will
                # either be created or rebased.
                ref_field_type = type(child).get_field(refdict.attr).type
                refname = ref_field_type.get_key_for_name(
                    schema, parent_fq_refname)

                astnode = ref_create_cmd.as_inherited_ref_ast(
                    schema, context, refname, self.scls)
                fq_name = self._classname_from_ast(schema, astnode, context)

                # We cannot check for ref existence in this child at this
                # time, because it might get created in a sibling branch
                # of the delta tree.  Instead, generate a command group
                # containing Alter(if_exists) and Create(if_not_exists)
                # to postpone that check until the application time.
                ref_create = ref_create_cmd.as_inherited_ref_cmd(
                    schema, context, astnode, [self.scls])
                ref_create.if_not_exists = True

                ref_create.set_attribute_value(refdict.backref_attr, child)

                if child.get_is_derived(schema):
                    # All references in a derived object must
                    # also be marked as derived, to be consistent
                    # with derive_subtype().
                    ref_create.set_attribute_value('is_derived', True)

                ref_alter = ref_alter_cmd(classname=fq_name, if_exists=True)
                ref_alter.add(ref_rebase_cmd(
                    classname=fq_name,
                    implicit=True,
                    added_bases=(),
                    removed_bases=(),
                ))

                alter.add(ref_alter)
                alter.add(ref_create)

            self.add(alter)

        return schema


class AlterReferencedInheritingObject(
    ReferencedInheritingObjectCommand[ReferencedInheritingObjectT],
    inheriting.AlterInheritingObject[ReferencedInheritingObjectT],
):

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        if self.get_attribute_value('is_from_alias'):
            return None
        else:
            return super()._get_ast(schema, context, parent_node=parent_node)

    @classmethod
    def _cmd_tree_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: sd.CommandContext,
    ) -> AlterReferencedInheritingObject[ReferencedInheritingObjectT]:
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)

        refctx = cls.get_referrer_context(context)
        if (
            refctx is not None
            and not qlast.has_ddl_subcommand(astnode, qlast.AlterOwned)
        ):
            cmd.set_attribute_value('is_owned', True)

        assert isinstance(cmd, AlterReferencedInheritingObject)
        return cmd

    def _alter_begin(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
    ) -> s_schema.Schema:
        scls = self.scls
        was_local = scls.get_is_owned(schema)
        schema = super()._alter_begin(schema, context)
        now_local = scls.get_is_owned(schema)
        if not was_local and now_local:
            self._validate(schema, context)
        return schema


class RebaseReferencedInheritingObject(
    ReferencedInheritingObjectCommand[ReferencedInheritingObjectT],
    inheriting.RebaseInheritingObject[ReferencedInheritingObjectT],
):

    implicit = struct.Field(bool, default=False)

    def apply(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
    ) -> s_schema.Schema:

        if not context.canonical and self.implicit:
            mcls = self.get_schema_metaclass()
            refctx = self.get_referrer_context_or_die(context)
            referrer = refctx.scls
            assert isinstance(referrer, so.InheritingObject)
            refdict = type(referrer).get_refdict_for_class(mcls)

            implicit_bases = self._get_implicit_ref_bases(
                schema,
                context,
                referrer=referrer,
                referrer_field=refdict.attr,
                fq_name=self.classname,
            )

            scls = self.get_object(schema, context)
            removed_bases, added_bases = self.get_ref_implicit_base_delta(
                schema,
                context,
                scls,
                implicit_bases=implicit_bases,
            )

            self.added_bases = added_bases
            self.removed_bases = removed_bases

        return super().apply(schema, context)

    def _get_bases_for_ast(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        bases: Tuple[so.ObjectShell, ...],
    ) -> Tuple[so.ObjectShell, ...]:
        bases = super()._get_bases_for_ast(schema, context, bases)
        implicit_bases = set(self.get_implicit_bases(schema, context, bases))
        return tuple(b for b in bases if b.name not in implicit_bases)


class RenameReferencedInheritingObject(
    ReferencedInheritingObjectCommand[ReferencedInheritingObjectT],
    sd.RenameObject[ReferencedInheritingObjectT],
):

    def _rename_begin(self,
                      schema: s_schema.Schema,
                      context: sd.CommandContext
                      ) -> s_schema.Schema:
        orig_schema = schema
        schema = super()._rename_begin(schema, context)
        scls = self.scls

        if not context.canonical and not scls.generic(schema):
            implicit_bases = scls.get_implicit_bases(schema)
            non_renamed_bases = set(implicit_bases) - context.renamed_objs

            # This object is inherited from one or more ancestors that
            # are not renamed in the same op, and this is an error.
            if non_renamed_bases:
                bases_str = ', '.join(
                    b.get_verbosename(schema, with_parent=True)
                    for b in non_renamed_bases
                )

                verb = 'are' if len(non_renamed_bases) > 1 else 'is'
                vn = scls.get_verbosename(orig_schema, with_parent=True)

                raise errors.SchemaDefinitionError(
                    f'cannot rename inherited {vn}',
                    details=(
                        f'{vn} is inherited from '
                        f'{bases_str}, which {verb} not being renamed'
                    ),
                    context=self.source_context,
                )

            schema = self._propagate_ref_rename(schema, context, scls)

        return schema

    def _propagate_ref_rename(self,
                              schema: s_schema.Schema,
                              context: sd.CommandContext,
                              scls: ReferencedInheritingObject
                              ) -> s_schema.Schema:
        rename_cmdcls = sd.ObjectCommandMeta.get_command_class_or_die(
            sd.RenameObject, type(scls))

        def _ref_rename(alter_cmd: sd.Command,
                        refname: str) -> None:
            astnode = rename_cmdcls.astnode(
                new_name=qlast.ObjectRef(
                    name=refname,
                ),
            )

            rename_cmd = rename_cmdcls._rename_cmd_from_ast(
                schema, astnode, context)

            alter_cmd.add(rename_cmd)

        return self._propagate_ref_op(schema, context, scls, cb=_ref_rename)


class DeleteReferencedInheritingObject(
    DeleteReferencedObjectCommand[ReferencedInheritingObjectT],
    inheriting.DeleteInheritingObject[ReferencedInheritingObjectT],
    ReferencedInheritingObjectCommand[ReferencedInheritingObjectT],
):

    def _delete_ref(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        referrer: so.Object,
    ) -> s_schema.Schema:

        scls = self.scls
        referrer_class = type(referrer)
        mcls = type(scls)
        refdict = referrer_class.get_refdict_for_class(mcls)
        reftype = referrer_class.get_field(refdict.attr).type
        refname = reftype.get_key_for(schema, self.scls)
        self_name = self.scls.get_name(schema)

        schema = referrer.del_classref(schema, refdict.attr, refname)

        if (isinstance(referrer, so.InheritingObject)
                and not context.canonical):

            if (not context.in_deletion(offset=1)
                    and not context.disable_dep_verification):
                implicit_bases = set(self._get_implicit_ref_bases(
                    schema, context, referrer, refdict.attr, self_name))

                deleted_bases = set()
                for ctx in context.stack:
                    if isinstance(ctx.op, type(self)):
                        deleted_bases.add(ctx.op.scls)

                implicit_bases -= deleted_bases

                if implicit_bases:
                    # Cannot remove inherited objects.
                    vn = scls.get_verbosename(schema, with_parent=True)
                    parents = [
                        b.get_field_value(schema, refdict.backref_attr)
                        for b in implicit_bases
                    ]

                    pnames = '\n- '.join(
                        p.get_verbosename(schema, with_parent=True)
                        for p in parents
                    )

                    raise errors.SchemaError(
                        f'cannot drop inherited {vn}',
                        context=self.source_context,
                        details=f'{vn} is inherited from:\n- {pnames}'
                    )

            for child in referrer.children(schema):
                assert isinstance(child, so.QualifiedObject)
                child_coll = child.get_field_value(schema, refdict.attr)
                fq_refname_in_child = self._classname_from_name(
                    self_name,
                    child.get_name(schema),
                )
                child_refname = reftype.get_key_for_name(
                    schema, fq_refname_in_child)
                existing = child_coll.get(schema, child_refname, None)

                if existing is not None:
                    alter = child.init_delta_command(schema, sd.AlterObject)
                    with alter.new_context(schema, context, child):
                        schema, cmd = self._propagate_ref_deletion(
                            schema, context, refdict, child, existing)
                        alter.add(cmd)
                    self.add(alter)

        return schema

    def _propagate_ref_deletion(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        refdict: so.RefDict,
        child: so.InheritingObject,
        child_ref: ReferencedInheritingObjectT,
    ) -> Tuple[s_schema.Schema, sd.Command]:
        name = child_ref.get_name(schema)
        implicit_bases = self._get_implicit_ref_bases(
            schema, context, child, refdict.attr, name)

        cmd: sd.Command

        if child_ref.get_is_owned(schema) or implicit_bases:
            # Child is either defined locally or is inherited
            # from another parent, so we need to do a rebase.
            removed_bases, added_bases = self.get_ref_implicit_base_delta(
                schema, context, child_ref, implicit_bases)

            rebase_cmd = child_ref.init_delta_command(
                schema,
                sd.AlterObject,
                added_bases=added_bases,
                removed_bases=removed_bases,
            )

            cmd = child_ref.init_delta_command(schema, sd.AlterObject)
            cmd.add(rebase_cmd)

        else:
            # The ref in child should no longer exist.
            cmd = child_ref.init_delta_command(schema, sd.DeleteObject)

        schema = cmd.apply(schema, context)

        return schema, cmd

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        refctx = type(self).get_referrer_context(context)
        if (
            refctx is not None
            and not self.get_orig_attribute_value('is_owned')
        ):
            return None
        else:
            return super()._get_ast(schema, context, parent_node=parent_node)


class AlterOwned(
    ReferencedInheritingObjectCommand[ReferencedInheritingObjectT],
    inheriting.AlterInheritingObjectFragment[ReferencedInheritingObjectT],
):

    _delta_action = 'alterowned'

    def _alter_begin(
        self,
        schema: s_schema.Schema,
        context: sd.CommandContext,
    ) -> s_schema.Schema:
        orig_schema = schema
        schema = super()._alter_begin(schema, context)
        scls = self.scls

        orig_owned = scls.get_is_owned(orig_schema)
        owned = scls.get_is_owned(schema)

        if (
            orig_owned != owned
            and not owned
            and not context.canonical
        ):
            implicit_bases = scls.get_implicit_bases(schema)
            if not implicit_bases:
                # ref isn't actually inherited, so cannot be un-owned
                vn = scls.get_verbosename(schema, with_parent=True)
                sn = type(scls).get_schema_class_displayname().upper()
                raise errors.InvalidDefinitionError(
                    f'cannot drop owned {vn}, as it is not inherited, '
                    f'use DROP {sn} instead',
                    context=self.source_context,
                )

            # DROP OWNED requires special handling: the object in question
            # must revert all modification made on top of inherited attributes.
            bases = scls.get_bases(schema).objects(schema)
            schema = self.inherit_fields(
                schema,
                context,
                bases,
                ignore_local=True,
            )

            for refdict in type(scls).get_refdicts():
                schema = self._drop_owned_refs(schema, context, refdict)

        return schema

    @classmethod
    def _cmd_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: sd.CommandContext,
    ) -> sd.ObjectCommand[ReferencedInheritingObjectT]:
        this_op = context.current().op
        assert isinstance(this_op, sd.ObjectCommand)
        return cls(classname=this_op.classname)

    @classmethod
    def _cmd_tree_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: sd.CommandContext,
    ) -> sd.Command:
        assert isinstance(astnode, qlast.AlterOwned)
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)
        cmd.set_attribute_value('is_owned', astnode.owned)
        return cmd
